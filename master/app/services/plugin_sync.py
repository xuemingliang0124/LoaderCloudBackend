"""插件同步服务：维护 Agent 与全局插件池的对齐。

三类事件触发同步：
- Agent 启动注册：响应附 expected_plugins，Agent diff 下载（见 agent_registry.register_by_ip）
- Master 上传/启用插件：broadcast_plugin_sync 推 MSG_PLUGIN_SYNC 给在线 Agent
- Master 禁用/删除插件：broadcast_plugin_remove 推 MSG_PLUGIN_REMOVE

Agent 端收到消息后异步下载/卸载，完成后回 MSG_PLUGIN_ACK，
本模块 on_plugin_ack 维护 agent_plugin 表。
"""

import hashlib
import json

from loguru import logger
from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.models.agent_plugin import AgentPlugin
from app.models.plugin import JmeterPlugin
from app.services import storage
from app.ws.manager import agent_manager
from app.ws.protocol import MSG_PLUGIN_REMOVE, MSG_PLUGIN_SYNC, Envelope


async def _get_enabled_plugins() -> list[JmeterPlugin]:
    """取所有 enabled 插件（Agent expected_plugins 清单）。"""
    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(JmeterPlugin).where(JmeterPlugin.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        # 解除 detach，避免 expire_on_commit 后访问失败
        for r in rows:
            await db.refresh(r)
        return list(rows)


async def _build_expected_payload(plugins: list[JmeterPlugin]) -> list[dict]:
    """构造 expected_plugins 响应体（含预签 URL）。"""
    out: list[dict] = []
    for p in plugins:
        url = await storage.presigned_get(p.file_key) if p.file_key else ""
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "version": p.version,
                "sha256": p.sha256,
                "size": p.size,
                "url": url,
            }
        )
    return out


async def broadcast_plugin_sync(plugin_id: int) -> int:
    """新插件上线/启用：给在线 Agent 推 install 消息。

    单插件推送（非全量），Agent 端按 sha256 决定是否下载。
    返回成功推送的 Agent 数。
    """
    async with SessionLocal() as db:
        plugin = await db.get(JmeterPlugin, plugin_id)
        if plugin is None or not plugin.enabled:
            return 0
        if not plugin.file_key:
            logger.warning(f"插件 {plugin_id} 缺少 file_key，跳过推送")
            return 0
        url = await storage.presigned_get(plugin.file_key)
        plugin_dict = {
            "id": plugin.id,
            "name": plugin.name,
            "version": plugin.version,
            "sha256": plugin.sha256,
            "size": plugin.size,
            "url": url,
        }

    payload = {"action": "install", "plugins": [plugin_dict]}
    sent = 0
    for agent_id in agent_manager.connected_ids():
        if await agent_manager.send(
            agent_id, Envelope.now(MSG_PLUGIN_SYNC, payload).model_dump()
        ):
            sent += 1
    logger.info(f"插件 {plugin_id} install 推送至 {sent} 个在线 Agent")
    return sent


async def broadcast_plugin_remove(sha256_list: list[str]) -> int:
    """插件禁用/删除：给在线 Agent 推 remove 消息。

    Agent 收到后扫描 plugin_dir，匹配 sha256 的 jar 删除。
    同时把 agent_plugin 表对应记录标记 pending_remove（任务结束后清理）。
    """
    if not sha256_list:
        return 0
    payload = {"action": "remove", "sha256_list": sha256_list}
    sent = 0
    for agent_id in agent_manager.connected_ids():
        if await agent_manager.send(
            agent_id, Envelope.now(MSG_PLUGIN_REMOVE, payload).model_dump()
        ):
            sent += 1

    # 标记 agent_plugin 表对应记录为 pending_remove（Agent 任务结束后清理）
    async with SessionLocal() as db:
        await db.execute(
            update_agent_plugin_status_by_sha(sha256_list, "pending_remove")
        )
        await db.commit()
    logger.info(
        f"插件 remove {sha256_list} 推送至 {sent} 个在线 Agent，"
        f"agent_plugin 记录置 pending_remove"
    )
    return sent


async def on_heartbeat(agent_id: str, plugin_hashes: list[str]) -> None:
    """心跳对账：Agent 上报 plugin_dir 实际 sha256 集合，Master 比对纠偏。

    差异集不为空时推一次 MSG_PLUGIN_SYNC（让 Agent 拉缺失的）+
    MSG_PLUGIN_REMOVE（让 Agent 删多余的，含已禁用的）。
    频率：每心跳周期一次，但仅在差异存在时下发（无差异 0 流量）。
    """
    if not plugin_hashes:
        # Agent 没装任何插件：若全局有 enabled 插件，推全量 sync
        expected = await _get_enabled_plugins()
        if expected:
            payload_plugins = await _build_expected_payload(expected)
            await agent_manager.send(
                agent_id,
                Envelope.now(
                    MSG_PLUGIN_SYNC,
                    {"action": "install", "plugins": payload_plugins},
                ).model_dump(),
            )
        return

    # 比对：expected（全局 enabled） vs reported（Agent 心跳上报）
    expected = await _get_enabled_plugins()
    expected_shas = {p.sha256 for p in expected}
    reported_set = set(plugin_hashes)

    missing_shas, extra_shas = _diff_sets(expected_shas, reported_set)

    if missing_shas:
        # 推 install：缺失的插件
        missing_plugins = [p for p in expected if p.sha256 in missing_shas]
        payload = await _build_expected_payload(missing_plugins)
        await agent_manager.send(
            agent_id,
            Envelope.now(
                MSG_PLUGIN_SYNC, {"action": "install", "plugins": payload}
            ).model_dump(),
        )
        logger.info(
            f"Agent[{agent_id}] 心跳对账：缺失 {len(missing_shas)} 个插件，已推 sync"
        )

    if extra_shas:
        # 推 remove：多余的插件（含 Master 已禁用/删除的）
        await agent_manager.send(
            agent_id,
            Envelope.now(
                MSG_PLUGIN_REMOVE,
                {"action": "remove", "sha256_list": list(extra_shas)},
            ).model_dump(),
        )
        logger.info(
            f"Agent[{agent_id}] 心跳对账：多余 {len(extra_shas)} 个插件，已推 remove"
        )


def _diff_sets(
    expected_shas: set[str], reported_shas: set[str]
) -> tuple[set[str], set[str]]:
    """计算插件差异集（纯函数，便于单测）。

    返回 (missing, extra)：
    - missing = expected - reported：Agent 缺失的，需推 install
    - extra = reported - expected：Agent 多余的（含已禁用），需推 remove
    """
    return expected_shas - reported_shas, reported_shas - expected_shas


async def on_plugin_ack(agent_id: str, plugins: list[dict]) -> None:
    """Agent 上报当前 plugin_dir 实际清单，刷新 agent_plugin 表。

    plugins: [{"name": "x.jar", "sha256": "...", "size": 123}]
    本方法负责：
    - 把 Agent 上报的 sha256 与 jmeter_plugin 表对齐，upsert agent_plugin
    - 本地有但 jmeter_plugin 表无的 → 标记 stale（不强制删，让 Agent 自查）
    """
    if not plugins:
        return
    reported_shas = {p.get("sha256", "") for p in plugins if p.get("sha256")}
    async with SessionLocal() as db:
        # 查 jmeter_plugin 表，建立 sha -> plugin_id 映射
        rows = (
            (
                await db.execute(
                    select(JmeterPlugin).where(JmeterPlugin.sha256.in_(reported_shas))
                )
            )
            .scalars()
            .all()
        )
        sha_to_plugin_id = {r.sha256: r.id for r in rows}

        # 删除 Agent 已上报但 jmeter_plugin 表无的记录（用户已删插件，Agent 也清掉了）
        # 注意：pending_remove 的记录不在这里删，等 Agent 任务结束触发
        existing = (
            (
                await db.execute(
                    select(AgentPlugin).where(AgentPlugin.agent_id == agent_id)
                )
            )
            .scalars()
            .all()
        )
        existing_shas = {r.installed_sha256: r for r in existing}

        for p in plugins:
            sha = p.get("sha256", "")
            plugin_id = sha_to_plugin_id.get(sha)
            if not plugin_id:
                continue
            rec = existing_shas.get(sha)
            if rec is None:
                db.add(
                    AgentPlugin(
                        agent_id=agent_id,
                        plugin_id=plugin_id,
                        installed_sha256=sha,
                        status="installed",
                    )
                )
            elif rec.status == "pending_remove":
                # Agent 已上报但本插件待清理：保留状态，等任务结束触发清理
                pass
            else:
                rec.status = "installed"
        await db.commit()
    logger.info(f"Agent[{agent_id}] plugin_ack 处理完成，上报 {len(plugins)} 个插件")


def update_agent_plugin_status_by_sha(sha_list: list[str], status: str):
    """构造 UPDATE 语句：把匹配 sha 的 agent_plugin 记录置指定状态。

    用 SQLAlchemy ORM 表达式确保跨方言兼容。
    """
    from sqlalchemy import update

    return (
        update(AgentPlugin)
        .where(AgentPlugin.installed_sha256.in_(sha_list))
        .values(status=status)
    )


async def expected_plugins_response() -> list[dict]:
    """Agent 注册端点响应用：取 enabled 插件 + 预签 URL。"""
    plugins = await _get_enabled_plugins()
    return await _build_expected_payload(plugins)


async def recover_legacy_script_plugins() -> int:
    """历史脚本级插件迁移：扫描 jmeter_script.plugins，
    按 sha256 去重落 jmeter_plugin 表（不删 script.plugins 字段）。

    一次性迁移工具，应用启动时调用；已迁移过的（sha256 命中）跳过。
    返回新建的插件记录数。
    """
    created = 0
    async with SessionLocal() as db:
        # Script ORM 已下线 plugins 属性（模型见 script.py 注释），但 DB 列保留；
        # 用裸 SQL 读历史数据，绕过 ORM 映射
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, plugins FROM jmeter_script WHERE plugins IS NOT NULL"
                    )
                )
            )
            .mappings()
            .all()
        )
        # 收集所有脚本插件项 [{key, filename}] 去重 by filename
        seen_filenames: dict[str, str] = {}  # filename -> minio_key
        for row in rows:
            raw = row["plugins"]
            # 裸 SQL 下 MySQL JSON 列由驱动返回字符串；sqlite 测试库为已解析 JSON
            items = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or [])
            for item in items or []:
                fn = item.get("filename") or ""
                key = item.get("key") or ""
                if fn and key and fn not in seen_filenames:
                    seen_filenames[fn] = key

        # 逐个从 MinIO 下载算 sha256，落 jmeter_plugin 表
        for fn, key in seen_filenames.items():
            # 先按 file_key 查是否已迁移（避免重复算 sha256）
            existing = (
                (
                    await db.execute(
                        select(JmeterPlugin).where(JmeterPlugin.file_key == key)
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                continue
            # 下载内容算 sha256（小文件，一次性读）
            try:
                obj_data = await storage.get_object_bytes(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"迁移脚本插件 {fn}（key={key}）失败: {exc}")
                continue

            sha = hashlib.sha256(obj_data).hexdigest()
            # sha256 去重：命中已有插件则跳过（不重复建记录）
            dup = (
                (
                    await db.execute(
                        select(JmeterPlugin).where(JmeterPlugin.sha256 == sha)
                    )
                )
                .scalars()
                .first()
            )
            if dup is not None:
                continue
            plugin = JmeterPlugin(
                name=fn,
                version="legacy",
                file_key=key,
                sha256=sha,
                size=len(obj_data),
                enabled=True,
                description="从历史脚本级插件迁移",
                created_by="system",
            )
            db.add(plugin)
            created += 1
        await db.commit()
    if created:
        logger.info(f"历史脚本插件迁移完成，新建 {created} 个全局插件记录")
    return created
