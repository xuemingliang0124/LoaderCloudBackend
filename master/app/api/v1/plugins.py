"""插件管理：jar 上传（MinIO 归档）+ 启用/禁用 + 删除 + 手动同步。

设计要点：
- sha256 内容指纹：上传前算，命中已有记录则复用 MinIO 对象，避免重复存储
- enabled=false 不进入 expected_plugins 清单，Agent 收 MSG_PLUGIN_REMOVE 后卸载
- 删除/禁用时联动 plugin_sync 推 remove 给在线 Agent
- 历史脚本级插件迁移在 service 层 recover_legacy_script_plugins 完成（一次性）
"""

import hashlib

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent_plugin import AgentPlugin
from app.models.plugin import JmeterPlugin
from app.schemas.common import ok
from app.services import storage
from app.services.exceptions import BusinessError

router = APIRouter()

_PLUGIN_EXT = ".jar"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@router.post("/plugins")
async def upload_plugin(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    version: str = Form(default="v1"),
    description: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> dict:
    """上传 JMeter 第三方插件 jar 到全局插件池。

    内容指纹去重：sha256 命中已有记录时，复用 MinIO 对象 + 增加版本说明，
    不重复上传 jar。
    """
    filename = file.filename or ""
    if not filename.lower().endswith(_PLUGIN_EXT):
        raise BusinessError("仅支持上传 .jar 文件", code=3201)
    data = await file.read()
    sha = _sha256_bytes(data)

    # sha256 去重：同内容不同文件名视为同一插件，返回既有记录
    existing = (
        (await db.execute(select(JmeterPlugin).where(JmeterPlugin.sha256 == sha)))
        .scalars()
        .first()
    )
    if existing is not None:
        # 同 sha 已存在：保持启用、刷新描述，不重新上传 MinIO 对象
        if description:
            existing.description = description
        if not existing.enabled:
            existing.enabled = True
        await db.commit()
        await db.refresh(existing)
        # 启用后需对在线 Agent 推同步（延迟 import 防循环）
        from app.services.plugin_sync import broadcast_plugin_sync

        await broadcast_plugin_sync(existing.id)
        return ok({"id": existing.id, "deduplicated": True})

    plugin = JmeterPlugin(
        name=name or filename,
        version=version,
        sha256=sha,
        size=len(data),
        description=description,
        created_by=user,
    )
    db.add(plugin)
    await db.flush()  # 先拿 id 组装 MinIO key
    object_key = f"plugins/{plugin.id}/{filename}"
    await storage.upload_bytes(
        object_key, data, content_type="application/java-archive"
    )
    plugin.file_key = object_key
    await db.commit()
    await db.refresh(plugin)

    # 新插件上线：对在线 Agent 推同步消息
    from app.services.plugin_sync import broadcast_plugin_sync

    await broadcast_plugin_sync(plugin.id)
    return ok({"id": plugin.id, "deduplicated": False})


@router.get("/plugins")
async def list_plugins(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    rows = (
        (await db.execute(select(JmeterPlugin).order_by(JmeterPlugin.id.desc())))
        .scalars()
        .all()
    )
    return ok(
        [
            {
                "id": r.id,
                "name": r.name,
                "version": r.version,
                "sha256": r.sha256,
                "size": r.size,
                "enabled": r.enabled,
                "description": r.description,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    )


@router.get("/plugins/{plugin_id}")
async def get_plugin(
    plugin_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    plugin = await db.get(JmeterPlugin, plugin_id)
    if plugin is None:
        raise BusinessError("插件不存在", code=3204)
    return ok(
        {
            "id": plugin.id,
            "name": plugin.name,
            "version": plugin.version,
            "file_key": plugin.file_key,
            "sha256": plugin.sha256,
            "size": plugin.size,
            "enabled": plugin.enabled,
            "description": plugin.description,
            "created_by": plugin.created_by,
        }
    )


@router.patch("/plugins/{plugin_id}")
async def update_plugin(
    plugin_id: int,
    enabled: bool | None = Form(default=None),
    description: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """启用/禁用、改描述。禁用时联动给在线 Agent 推 remove。"""
    plugin = await db.get(JmeterPlugin, plugin_id)
    if plugin is None:
        raise BusinessError("插件不存在", code=3204)
    if enabled is not None and enabled != plugin.enabled:
        plugin.enabled = enabled
    if description is not None:
        plugin.description = description
    await db.commit()
    await db.refresh(plugin)

    # 启用 → 推 install；禁用 → 推 remove（延迟 import 防循环）
    from app.services.plugin_sync import (
        broadcast_plugin_remove,
        broadcast_plugin_sync,
    )

    if enabled is True:
        await broadcast_plugin_sync(plugin_id)
    elif enabled is False:
        await broadcast_plugin_remove([plugin.sha256])

    return ok({"id": plugin.id, "enabled": plugin.enabled})


@router.delete("/plugins/{plugin_id}")
async def delete_plugin(
    plugin_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """删除插件：先推 remove 给在线 Agent，再清 agent_plugin 关联，最后删 jmeter_plugin。

    顺序约束：agent_plugin.plugin_id 有外键指向 jmeter_plugin.id，
    必须先删子表（agent_plugin）再删父表（jmeter_plugin），否则 1451 报错。
    MinIO 对象保留做审计。
    """
    plugin = await db.get(JmeterPlugin, plugin_id)
    if plugin is None:
        raise BusinessError("插件不存在", code=3204)
    sha = plugin.sha256

    # 1. 先给在线 Agent 推卸载消息（让 Agent 删本地 jar）
    from app.services.plugin_sync import broadcast_plugin_remove

    await broadcast_plugin_remove([sha])

    # 2. 删 agent_plugin 关联记录（子表先清，避免外键约束 1451）
    from sqlalchemy import delete

    await db.execute(delete(AgentPlugin).where(AgentPlugin.plugin_id == plugin_id))
    # 3. 删 jmeter_plugin 主记录（父表）
    await db.delete(plugin)
    await db.commit()
    return ok({"id": plugin_id, "deleted": True})


@router.post("/plugins/{plugin_id}/sync")
async def sync_plugin(
    plugin_id: int,
    _: str = Depends(get_current_user),
) -> dict:
    """手动触发对全部在线 Agent 同步某插件（运营兜底）。"""
    from app.services.plugin_sync import broadcast_plugin_sync

    sent = await broadcast_plugin_sync(plugin_id)
    return ok({"plugin_id": plugin_id, "pushed_to": sent})
