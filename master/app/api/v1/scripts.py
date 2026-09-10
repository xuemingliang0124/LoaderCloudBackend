"""脚本管理：JMX 上传（MinIO 归档）+ 列表。"""

import json

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.script import Script
from app.schemas import ScriptOut
from app.schemas.common import ok
from app.services import jmx_checker, storage
from app.services.exceptions import BusinessError

router = APIRouter()

# 数据文件允许的扩展名（JMX 内 CSV Data Set Config / Random CSV 等引用的文本数据）
_DATA_FILE_EXTS = (".csv", ".txt", ".dat", ".tsv")
# 第三方 JMeter 插件扩展名
_PLUGIN_EXTS = (".jar",)


@router.post("/scripts")
async def upload_script(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    version: str = Form(default="v1"),
    description: str = Form(default=""),
    params: str = Form(default="[]"),
    data_files: list[UploadFile] = File(default=[]),
    plugin_files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> dict:
    """上传 JMX 脚本及关联的数据文件、第三方插件 jar。

    params 为占位符定义 JSON：[{"key","default","desc"}]。
    data_files 为 JMX 引用的 CSV 等数据文件，文件名必须与 JMX 内引用一致。
    plugin_files 为 JMX 使用的第三方插件 jar（文件名即插件标识）；下发时校验
    Agent 已装，缺失的随任务下发到 Agent plugin_dir，免改镜像。
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

    # 校验插件 jar 扩展名 + 去重
    clean_plugins: list[UploadFile] = []
    for pf in plugin_files:
        pf_name = pf.filename or ""
        if not pf_name:
            raise BusinessError("插件文件缺少 filename", code=3007)
        if not pf_name.lower().endswith(_PLUGIN_EXTS):
            raise BusinessError(f"插件仅支持 .jar: {pf_name}", code=3007)
        if pf_name in seen:
            raise BusinessError(f"插件文件重名: {pf_name}", code=3008)
        seen.add(pf_name)
        clean_plugins.append(pf)

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

    # 插件 jar 归档到 plugins/ 子目录；filename 即插件标识，供下发时比对 Agent 已装清单
    plugin_attachments: list[dict] = []
    for pf in clean_plugins:
        pf_name = pf.filename or ""
        pf_key = f"scripts/{script.id}/{version}/plugins/{pf_name}"
        pf_data = await pf.read()
        await storage.upload_bytes(
            pf_key, pf_data, content_type="application/java-archive"
        )
        plugin_attachments.append({"key": pf_key, "filename": pf_name})
    script.plugins = plugin_attachments or None

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
