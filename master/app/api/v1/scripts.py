"""脚本管理：JMX 上传（MinIO 归档）+ 列表。

插件管理已迁移到 /api/v1/plugins 端点（全局插件池），
脚本上传不再带 plugin_files 参数。
"""

import dataclasses
import json

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.script import Script
from app.schemas import ScriptOut
from app.schemas.common import ok
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
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    rows = (await db.execute(select(Script).order_by(Script.id.desc()))).scalars().all()
    return ok([ScriptOut.model_validate(r).model_dump(mode="json") for r in rows])


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
