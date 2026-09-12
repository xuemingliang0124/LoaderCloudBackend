"""脚本管理：JMX 上传（MinIO 归档）+ 列表。

插件管理已迁移到 /api/v1/plugins 端点（全局插件池），
脚本上传不再带 plugin_files 参数。
"""

import dataclasses
import json

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.script import Script
from app.schemas import ScriptOut
from app.schemas.common import like_pattern, ok
from app.schemas.jmx_scan import JmxScanOut
from app.services import jmx_checker, jmx_scanner, storage
from app.services.exceptions import BusinessError

router = APIRouter()

# 数据文件允许的扩展名（JMX 内 CSV Data Set Config / Random CSV 等引用的文本数据）
_DATA_FILE_EXTS = (".csv", ".txt", ".dat", ".tsv")


@router.post("/scripts")
async def upload_script(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    version: str = Form(default="v1"),
    description: str = Form(default=""),
    params: str = Form(default="[]"),
    data_files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> dict:
    """上传 JMX 脚本及关联的数据文件。

    params 为占位符定义 JSON：[{"key","default","desc"}]。
    data_files 为 JMX 引用的 CSV 等数据文件，文件名必须与 JMX 内引用一致。
    第三方插件不再随脚本上传：改用 POST /api/v1/plugins 上传到全局插件池，
    Agent 启动/在线推送时由 PluginSyncer 对齐 plugin_dir。
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".jmx"):
        raise BusinessError("仅支持上传 .jmx 文件", code=3001)
    try:
        param_list = json.loads(params)
    except json.JSONDecodeError as exc:
        raise BusinessError("params 不是合法 JSON", code=3002) from exc

    # 校验数据文件扩展名 + 去重（按 filename）
    seen: set[str] = set()
    clean_data_files: list[UploadFile] = []
    for df in data_files:
        df_name = df.filename or ""
        if not df_name:
            raise BusinessError("数据文件缺少 filename", code=3003)
        if not df_name.lower().endswith(_DATA_FILE_EXTS):
            raise BusinessError(
                f"数据文件仅支持 {'/'.join(_DATA_FILE_EXTS)}: {df_name}", code=3003
            )
        if df_name in seen:
            raise BusinessError(f"数据文件重名: {df_name}", code=3004)
        seen.add(df_name)
        clean_data_files.append(df)

    jmx_data = await file.read()

    # 校验 JMX 引用的 CSV 数据文件是否已全部上传
    uploaded_names = {df.filename or "" for df in clean_data_files}
    missing = jmx_checker.check_data_files_complete(jmx_data, uploaded_names)
    if missing:
        raise BusinessError(f"缺少数据文件: {', '.join(missing)}", code=3006)

    script = Script(
        name=name or filename.rsplit(".", 1)[0],
        version=version,
        params=param_list,
        description=description,
        created_by=user,
    )
    db.add(script)
    await db.flush()  # 先拿 id 组装 MinIO key
    object_key = f"scripts/{script.id}/{version}/{filename}"
    await storage.upload_bytes(object_key, jmx_data, content_type="text/xml")
    script.file_key = object_key

    # 数据文件归档到同目录，filename 作为 save_as 的来源
    attachments: list[dict] = []
    for df in clean_data_files:
        df_name = df.filename or ""
        df_key = f"scripts/{script.id}/{version}/{df_name}"
        df_data = await df.read()
        await storage.upload_bytes(df_key, df_data, content_type="text/csv")
        attachments.append({"key": df_key, "filename": df_name})
    script.data_files = attachments or None

    await db.commit()
    await db.refresh(script)
    return ok(ScriptOut.model_validate(script).model_dump(mode="json"))


@router.get("/scripts")
async def list_scripts(
    name: str | None = Query(default=None, description="按脚本名模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    filters = []
    if name:
        filters.append(Script.name.like(like_pattern(name.strip()), escape="\\"))

    total = await db.scalar(select(func.count()).select_from(Script).where(*filters))
    rows = (
        (
            await db.execute(
                select(Script)
                .where(*filters)
                .order_by(Script.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [ScriptOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.put("/scripts/{script_id}/jmx")
async def replace_script_jmx(
    script_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """上传新 JMX 替换脚本文件。

    上传前比对新旧文件的启用线程组（名称 + 类型），必须完全一致才允许替换，
    否则返回失败并给出差异明细（场景线程组设置按名称落库，标识变化会导致
    设置失效）。线程数/rampUp/循环/持续时间等参数允许不同，执行时由场景设置覆盖。
    替换在原 MinIO 对象上覆盖，file_key、数据文件与场景关联均不变。
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".jmx"):
        raise BusinessError("仅支持上传 .jmx 文件", code=3001)

    script = (
        await db.execute(select(Script).where(Script.id == script_id))
    ).scalar_one_or_none()
    if script is None:
        raise BusinessError("脚本不存在", code=3007)
    if not script.file_key:
        raise BusinessError("原脚本缺少 JMX 文件，无法替换", code=3008)

    new_bytes = await file.read()
    # fail-fast：先解析新文件，非法 XML 直接拒绝（scan_jmx 抛 BusinessError 3005）
    new_groups = jmx_scanner.scan_jmx(new_bytes).thread_groups

    old_bytes = await storage.get_object_bytes(script.file_key)
    old_groups = jmx_scanner.scan_jmx(old_bytes).thread_groups

    diff = jmx_scanner.compare_thread_groups(old_groups, new_groups)
    if not diff.is_consistent:
        raise BusinessError(
            f"新脚本与原脚本线程组不一致，拒绝替换：{diff.describe()}", code=3009
        )

    # 线程组一致：覆盖原对象（路径不变，数据文件/场景关联不受影响）
    await storage.upload_bytes(script.file_key, new_bytes, content_type="text/xml")
    return ok(ScriptOut.model_validate(script).model_dump(mode="json"))


@router.delete("/scripts/{script_id}")
async def delete_script(
    script_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """删除脚本：校验场景引用 → 删 jmeter_script → 清理 MinIO 的 JMX 与参数文件。

    scenario_script.script_id 外键没有 cascade，被场景引用时拒绝删除，
    需先在场景中移除该脚本（否则直接删会触发外键 1451）。
    MinIO 对象在删库后清理，失败仅告警不阻断，残留对象由运维定期清理。
    """
    script = (
        await db.execute(select(Script).where(Script.id == script_id))
    ).scalar_one_or_none()
    if script is None:
        raise BusinessError("脚本不存在", code=3007)

    # 删库前留档所有对象 key（JMX + 参数文件）
    object_keys = [script.file_key] if script.file_key else []
    object_keys.extend(
        df["key"]
        for df in (script.data_files or [])
        if isinstance(df, dict) and df.get("key")
    )

    # 被场景引用时拒绝删除（懒 import 防模型循环引用）
    from app.models.scenario import Scenario
    from app.models.scenario_script import ScenarioScript

    scenario_names = (
        (
            await db.execute(
                select(Scenario.name)
                .join(ScenarioScript, ScenarioScript.scenario_id == Scenario.id)
                .where(ScenarioScript.script_id == script_id)
            )
        )
        .scalars()
        .all()
    )
    if scenario_names:
        preview = ", ".join(scenario_names[:5])
        more = " 等" if len(scenario_names) > 5 else ""
        raise BusinessError(
            f"脚本已被 {len(scenario_names)} 个场景引用，请先在场景中移除：{preview}{more}",
            code=3010,
        )

    await db.delete(script)
    await db.commit()

    for key in object_keys:
        try:
            await storage.delete_object(key)
        except Exception as exc:  # noqa: BLE001
            from loguru import logger

            logger.warning(f"脚本 {script_id} MinIO 对象删除失败 ({key}): {exc}")

    return ok({"id": script_id, "deleted": True})


@router.get("/scripts/{script_id}/thread-groups")
async def get_thread_groups(
    script_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """扫描脚本 JMX，返回线程组参数（前端展示与修改用）。

    逐层校验 enabled，禁用链路下的线程组不返回；线程组参数中的用户变量
    引用已按"线程组内变量 > 全局变量"作用域解析，无法静态解析的字段取 0。
    """
    script = (
        await db.execute(select(Script).where(Script.id == script_id))
    ).scalar_one_or_none()
    if script is None:
        raise BusinessError("脚本不存在", code=3007)
    jmx_bytes = await storage.get_object_bytes(script.file_key)
    result = jmx_scanner.scan_jmx(jmx_bytes)
    return ok(JmxScanOut(**dataclasses.asdict(result)).model_dump(mode="json"))
