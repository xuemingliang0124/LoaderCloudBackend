"""文档资产管理：上传（MinIO 归档 + hash 去重 + 投递解析）/ 列表 / 详情 / 更新 / 删除 / 重试解析。

全部资产接口以项目为作用域，统一使用 /projects/{project_id}/assets 嵌套路由：
- 项目门禁：editor+ 上传/更新/删除/重试，viewer+ 查询
- 上传即入库（status=PENDING），文件本体存 MinIO `assets/{asset_id}/{filename}`，
  提交后延迟投递 D3 解析任务（APScheduler，不阻塞上传响应）
- 去重：同项目内 hash_sha256 重复直接复用原 asset_id（不重复存 MinIO、不重复解析）
- 文件类型与 asset_type 约束：扩展名必须匹配该类型允许的格式（3060）
- 操作具体资产时校验归属：不存在 3061，不属于该项目 3062
- 重试解析仅 pending/failed 可触发（3063），解析状态机见 services/asset_parser.py
"""

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.core.config import ERR_VECTOR_STORE_UNAVAILABLE
from app.db.session import get_db
from app.models.asset import Asset
from app.models.enums import AssetStatus, AssetType
from app.schemas import (
    AssetOut,
    AssetUpdateIn,
    KnowledgeSearchItemOut,
    KnowledgeSearchOut,
)
from app.schemas.common import like_pattern, ok
from app.services import storage
from app.services.asset_parser import schedule_asset_parse
from app.services.exceptions import BusinessError
from app.services.llm.client import format_citation
from app.services.llm.orchestrator import search_knowledge

router = APIRouter()

# 资产类型 → 允许的文件扩展名（小写，含点）：上传与类型变更时校验，
# 保证 D3 解析管道能按扩展名分发到对应解析器
_ASSET_TYPE_EXTENSIONS: dict[AssetType, tuple[str, ...]] = {
    AssetType.PLAN_DOC: (".docx", ".doc", ".pdf"),
    AssetType.ENV_INVENTORY: (".xlsx", ".xls"),
    AssetType.TXN_INVENTORY: (".xlsx", ".xls"),
    AssetType.SLA_DOC: (".docx", ".doc", ".pdf"),
    AssetType.ARCHITECTURE_DOC: (".docx", ".doc", ".pdf", ".pptx"),
}


def _allowed_extensions(asset_type: AssetType) -> tuple[str, ...]:
    return _ASSET_TYPE_EXTENSIONS[asset_type]


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _check_extension(filename: str, asset_type: AssetType) -> None:
    """校验文件扩展名与资产类型匹配，不匹配返回 3060。"""
    ext = _ext_of(filename)
    allowed = _allowed_extensions(asset_type)
    if ext not in allowed:
        raise BusinessError(
            f"资产类型 {asset_type.value} 仅支持 {', '.join(allowed)} 文件，"
            f"当前文件扩展名为 {ext or '（无）'}",
            code=3060,
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_name(filename: str) -> str:
    """上传未指定 name 时，取文件名去扩展名。"""
    stem = Path(filename or "").stem
    return stem or filename or "未命名资产"


async def _get_scoped_asset(db: AsyncSession, project_id: int, asset_id: int) -> Asset:
    """按项目作用域取资产：不存在 3061，跨项目访问 3062。"""
    asset = (
        await db.execute(select(Asset).where(Asset.id == asset_id))
    ).scalar_one_or_none()
    if asset is None:
        raise BusinessError("资产不存在", code=3061)
    if asset.project_id != project_id:
        raise BusinessError("资产不属于指定项目", code=3062)
    return asset


@router.post("/projects/{project_id}/assets")
async def upload_asset(
    project_id: int,
    file: UploadFile = File(...),
    asset_type: AssetType = Form(...),
    name: str | None = Form(default=None, max_length=256),
    description: str = Form(default="", max_length=512),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """上传文档资产（editor+）：落地 MinIO + 入库 PENDING，同 hash 复用。

    - asset_type 为枚举，非法值由 FastAPI 校验返回 422
    - 文件扩展名必须匹配 asset_type 允许的格式（3060）
    - 同项目内 hash_sha256 重复：返回原资产，reused=true，不重复存 MinIO
    - 新文件：MinIO key=assets/{asset_id}/{filename}，status=PENDING
    """
    await ensure_project_access(db, project_id, user, "editor")

    filename = file.filename or ""
    if not filename:
        raise BusinessError("上传文件缺少 filename", code=3060)
    _check_extension(filename, asset_type)

    data = await file.read()
    digest = _sha256(data)

    # 去重：同项目同 hash 直接复用（不重复上传 MinIO）
    existing = (
        await db.execute(
            select(Asset).where(
                Asset.project_id == project_id, Asset.hash_sha256 == digest
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        out = AssetOut.model_validate(existing).model_dump(mode="json")
        out["reused"] = True
        return ok(out)

    # name 去首尾空白；纯空白按 None 处理，回退为文件名去扩展名
    clean_name = name.strip() if name else None
    asset = Asset(
        project_id=project_id,
        name=clean_name or _default_name(filename),
        asset_type=asset_type.value,
        status=AssetStatus.PENDING.value,
        filename=filename,
        hash_sha256=digest,
        file_size=len(data),
        content_type=file.content_type or "",
        description=description,
        created_by=user.username,
    )
    db.add(asset)
    await db.flush()  # 先拿 id 组装 MinIO key
    object_key = f"assets/{asset.id}/{filename}"
    await storage.upload_bytes(object_key, data, content_type=asset.content_type)
    asset.file_key = object_key

    await db.commit()
    await db.refresh(asset)
    # 新文件投递 D3 解析任务（调度器未启动时仅告警，可经 retry-parse 手动触发）
    schedule_asset_parse(asset.id)
    out = AssetOut.model_validate(asset).model_dump(mode="json")
    out["reused"] = False
    return ok(out)


@router.get("/projects/{project_id}/assets")
async def list_assets(
    project_id: int,
    asset_type: str | None = Query(default=None, description="按资产类型过滤"),
    status: str | None = Query(default=None, description="按解析状态过滤"),
    name: str | None = Query(default=None, description="按资产名称模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """资产分页列表（viewer+）：支持类型/状态/名称过滤，按 id 倒序，响应含 total。"""
    await ensure_project_access(db, project_id, user, "viewer")

    filters = [Asset.project_id == project_id]
    if asset_type:
        filters.append(Asset.asset_type == asset_type.strip())
    if status:
        filters.append(Asset.status == status.strip())
    if name:
        filters.append(Asset.name.like(like_pattern(name.strip()), escape="\\"))

    total = await db.scalar(select(func.count()).select_from(Asset).where(*filters))
    rows = (
        (
            await db.execute(
                select(Asset)
                .where(*filters)
                .order_by(Asset.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [AssetOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.get("/projects/{project_id}/assets/{asset_id}")
async def get_asset(
    project_id: int,
    asset_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """资产详情（viewer+）：不存在 3061，跨项目 3062。"""
    await ensure_project_access(db, project_id, user, "viewer")
    asset = await _get_scoped_asset(db, project_id, asset_id)
    return ok(AssetOut.model_validate(asset).model_dump(mode="json"))


@router.put("/projects/{project_id}/assets/{asset_id}")
async def update_asset(
    project_id: int,
    asset_id: int,
    payload: AssetUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新资产元数据（editor+）：仅名称/描述/类型，文件本体不可替换。

    asset_type 变更后须与现有文件扩展名匹配（3060）；
    name/description 为空或与原值相同的字段保持不变。
    """
    await ensure_project_access(db, project_id, user, "editor")
    asset = await _get_scoped_asset(db, project_id, asset_id)

    if payload.asset_type is not None:
        _check_extension(asset.filename, payload.asset_type)
        asset.asset_type = payload.asset_type.value
    if payload.name is not None:
        asset.name = payload.name
    if payload.description is not None:
        asset.description = payload.description

    await db.commit()
    await db.refresh(asset)
    return ok(AssetOut.model_validate(asset).model_dump(mode="json"))


@router.delete("/projects/{project_id}/assets/{asset_id}")
async def delete_asset(
    project_id: int,
    asset_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除资产（editor+）：删库行 + best-effort 清理 MinIO 对象。

    与脚本删除口径一致：MinIO 对象在删库后清理，失败仅告警不阻断，
    残留对象由运维定期清理。
    """
    await ensure_project_access(db, project_id, user, "editor")
    asset = await _get_scoped_asset(db, project_id, asset_id)

    object_key = asset.file_key
    await db.delete(asset)
    await db.commit()

    if object_key:
        try:
            await storage.delete_object(object_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"资产 {asset_id} MinIO 对象删除失败 ({object_key}): {exc}")

    return ok({"id": asset_id, "project_id": project_id, "deleted": True})


@router.post("/projects/{project_id}/assets/{asset_id}/retry-parse")
async def retry_asset_parse(
    project_id: int,
    asset_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """重试解析（editor+）：pending/failed 资产重新投递 D3 解析任务。

    - parsing/ready 状态拒绝（3063）：解析中防重入，已完成请勿重复解析
    - 重试不重传 MinIO 文件，仅重置状态并延迟投递解析任务
    """
    await ensure_project_access(db, project_id, user, "editor")
    asset = await _get_scoped_asset(db, project_id, asset_id)
    if asset.status not in (AssetStatus.PENDING.value, AssetStatus.FAILED.value):
        raise BusinessError(
            f"资产当前状态为 {asset.status}，仅 pending/failed 状态可重试解析",
            code=3063,
        )
    asset.status = AssetStatus.PENDING.value
    asset.parse_meta = {}
    await db.commit()
    schedule_asset_parse(asset.id)
    return ok({"id": asset.id, "status": asset.status, "queued": True})


@router.get("/assets/knowledge-search")
async def knowledge_search(
    project_id: int = Query(..., ge=1),
    q: str = Query(..., min_length=1, max_length=2000, description="检索词"),
    top_k: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """RAG 知识检索（viewer+，FR-05/FR-10）：返回 ≤top_k 命中切片与相似度分。

    非项目嵌套路径（SRS 6.1：GET /api/v1/assets/knowledge-search），project_id
    走 query 参数并强制成员门禁；检索词纯空白 → 422；向量库不可用 → 4003。
    """
    await ensure_project_access(db, project_id, user, "viewer")
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="检索词不能为纯空白")

    try:
        pairs = await search_knowledge(project_id, query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"知识检索失败（向量库不可用）: {type(exc).__name__}: {exc}")
        raise BusinessError(
            "向量库不可用，知识检索失败", code=ERR_VECTOR_STORE_UNAVAILABLE
        )

    items: list[KnowledgeSearchItemOut] = []
    for doc, score in pairs:
        meta = doc.metadata or {}
        items.append(
            KnowledgeSearchItemOut(
                citation=format_citation(doc),
                content=doc.page_content,
                score=round(float(score), 4),
                asset_id=meta.get("asset_id"),
                asset_type=meta.get("asset_type") or "unknown",
                chunk_index=int(meta.get("chunk_index") or 0),
            )
        )
    result = KnowledgeSearchOut(total=len(items), items=items)
    return ok(result.model_dump())
