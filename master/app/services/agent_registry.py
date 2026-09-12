"""Agent 注册中心：注册、心跳、超时判定、选机调度。"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.agent_node import AgentNode
from app.models.agent_plugin import AgentPlugin
from app.models.enums import AgentStatus
from app.models.plugin import JmeterPlugin
from app.schemas.common import like_pattern


async def _sync_agent_plugin_records(
    db: AsyncSession,
    agent_id: str,
    reported_plugins: list[dict] | None,
) -> None:
    """刷新 agent_plugin 表：Agent 上报的 plugins 与全局插件池对齐。

    plugins 元素结构 [{name, sha256, size}]：
    - 上报 sha 在 jmeter_plugin 表命中 → upsert installed
    - 上报 sha 无对应全局插件 → 忽略（可能用户删了插件，Agent 还残留）
    - 全局 enabled 但 Agent 没报 → 不在此处补装，由 PluginSyncer 推 install
    """
    if not reported_plugins:
        return
    reported_shas = {p.get("sha256", "") for p in reported_plugins if p.get("sha256")}
    if not reported_shas:
        return

    # 查全局插件表：sha -> plugin_id
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

    # 查 Agent 既有记录
    existing = (
        (await db.execute(select(AgentPlugin).where(AgentPlugin.agent_id == agent_id)))
        .scalars()
        .all()
    )
    existing_by_plugin_id = {r.plugin_id: r for r in existing}

    for p in reported_plugins:
        sha = p.get("sha256", "")
        plugin_id = sha_to_plugin_id.get(sha)
        if not plugin_id:
            continue
        rec = existing_by_plugin_id.get(plugin_id)
        if rec is None:
            db.add(
                AgentPlugin(
                    agent_id=agent_id,
                    plugin_id=plugin_id,
                    installed_sha256=sha,
                    status="installed",
                )
            )
        else:
            rec.installed_sha256 = sha
            if rec.status == "pending_remove":
                # 重新装回了，重置为 installed
                rec.status = "installed"


async def register_by_ip(
    ip: str,
    hostname: str = "",
    tags: list[str] | None = None,
    jmeter_version: str = "",
    plugins: list[dict] | None = None,
    cpu_cores: int = 0,
    mem_total_gb: float = 0.0,
) -> tuple[AgentNode, bool]:
    """Agent 启动注册：按宿主机 IP 查固定 agent_id。

    - IP 已存在：返回既有节点（agent_id 保持不变），顺带刷新 hostname/tags/版本/规格
    - IP 不存在：生成 agent-<uuid8> 新建节点（OFFLINE，WS 握手后转 ONLINE）

    plugins 元素结构：[{name, sha256, size}]（Agent plugin_dir 实际清单，
    本字段为快照冗余，权威数据在 agent_plugin 表）

    返回 (节点, is_new)。
    """
    async with SessionLocal() as db:
        node = (
            (await db.execute(select(AgentNode).where(AgentNode.ip == ip)))
            .scalars()
            .first()
        )
        if node is not None:
            node.hostname = hostname or node.hostname
            if tags:
                node.tags = tags
            node.jmeter_version = jmeter_version or node.jmeter_version
            if plugins:
                node.plugins = plugins
            if cpu_cores:
                node.cpu_cores = cpu_cores
            if mem_total_gb:
                node.mem_total_gb = mem_total_gb
            await _sync_agent_plugin_records(db, node.agent_id, plugins)
            await db.commit()
            await db.refresh(node)
            return node, False

        node = AgentNode(
            agent_id=f"agent-{uuid.uuid4().hex[:8]}",
            ip=ip,
            hostname=hostname,
            tags=tags or [],
            jmeter_version=jmeter_version,
            plugins=plugins or [],
            cpu_cores=cpu_cores,
            mem_total_gb=mem_total_gb,
            status=AgentStatus.OFFLINE,
        )
        db.add(node)
        await db.flush()  # 确保 agent_id 可用
        await _sync_agent_plugin_records(db, node.agent_id, plugins)
        await db.commit()
        await db.refresh(node)
        return node, True


async def upsert_agent(
    agent_id: str,
    ip: str = "",
    hostname: str = "",
    tags: list[str] | None = None,
    jmeter_version: str = "",
    plugins: list[dict] | None = None,
    cpu_cores: int = 0,
    mem_total_gb: float = 0.0,
) -> None:
    """Agent 注册/上线刷新（WS 握手时调用）。"""
    async with SessionLocal() as db:
        node = (
            (await db.execute(select(AgentNode).where(AgentNode.agent_id == agent_id)))
            .scalars()
            .first()
        )
        if node is None:
            db.add(
                AgentNode(
                    agent_id=agent_id,
                    ip=ip,
                    hostname=hostname,
                    tags=tags or [],
                    jmeter_version=jmeter_version,
                    plugins=plugins or [],
                    cpu_cores=cpu_cores,
                    mem_total_gb=mem_total_gb,
                    status=AgentStatus.ONLINE,
                    last_heartbeat=datetime.now(),
                )
            )
        else:
            node.ip = ip or node.ip
            node.hostname = hostname or node.hostname
            node.tags = tags or node.tags
            node.jmeter_version = jmeter_version or node.jmeter_version
            if plugins:
                node.plugins = plugins
            if cpu_cores:
                node.cpu_cores = cpu_cores
            if mem_total_gb:
                node.mem_total_gb = mem_total_gb
            node.status = AgentStatus.ONLINE
            node.last_heartbeat = datetime.now()
            await _sync_agent_plugin_records(db, agent_id, plugins)
        await db.commit()


async def touch_heartbeat(
    agent_id: str,
    cpu: float = 0.0,
    mem: float = 0.0,
    current_run_no: str | None = None,
    cpu_cores: int = 0,
    mem_total_gb: float = 0.0,
) -> None:
    """心跳落库：按是否在跑任务区分 ONLINE / BUSY；规格字段有值才刷新。"""
    values: dict = dict(
        cpu_percent=cpu,
        mem_percent=mem,
        current_run_no=current_run_no,
        status=AgentStatus.BUSY if current_run_no else AgentStatus.ONLINE,
        last_heartbeat=datetime.now(),
    )
    if cpu_cores:
        values["cpu_cores"] = cpu_cores
    if mem_total_gb:
        values["mem_total_gb"] = mem_total_gb
    async with SessionLocal() as db:
        await db.execute(
            update(AgentNode).where(AgentNode.agent_id == agent_id).values(**values)
        )
        await db.commit()


async def mark_offline(agent_id: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(AgentNode)
            .where(AgentNode.agent_id == agent_id)
            .values(status=AgentStatus.OFFLINE, current_run_no=None)
        )
        await db.commit()


async def mark_stale_agents_offline(connected_ids: set[str]) -> int:
    """扫描心跳超时（连续 N 个周期未上报）且当前无连接的 Agent，置 OFFLINE。"""
    settings = get_settings()
    cutoff = datetime.now() - timedelta(
        seconds=settings.agent_heartbeat_interval * settings.agent_offline_threshold
    )
    async with SessionLocal() as db:
        stmt = (
            update(AgentNode)
            .where(
                AgentNode.status != AgentStatus.OFFLINE,
                AgentNode.last_heartbeat < cutoff,
                AgentNode.agent_id.not_in(connected_ids) if connected_ids else True,
            )
            .values(status=AgentStatus.OFFLINE, current_run_no=None)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount or 0


async def list_agents(
    db: AsyncSession,
    *,
    keyword: str | None = None,
    status: AgentStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[AgentNode], int]:
    """分页查询压力机。

    keyword 非空时模糊匹配 agent_id / ip / hostname（OR 语义）；
    status 非空时按在线状态精确过滤。返回 (当前页节点, 总数)。
    """
    filters = []
    if keyword:
        pattern = like_pattern(keyword.strip())
        filters.append(
            or_(
                AgentNode.agent_id.like(pattern, escape="\\"),
                AgentNode.ip.like(pattern, escape="\\"),
                AgentNode.hostname.like(pattern, escape="\\"),
            )
        )
    if status is not None:
        filters.append(AgentNode.status == status)

    total = await db.scalar(select(func.count()).select_from(AgentNode).where(*filters))
    result = await db.execute(
        select(AgentNode)
        .where(*filters)
        .order_by(AgentNode.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), int(total or 0)


async def get_nodes(agent_ids: list[str]) -> dict[str, AgentNode]:
    """按 agent_id 批量取节点（插件/规格等调度信息）。"""
    if not agent_ids:
        return {}
    async with SessionLocal() as db:
        result = await db.execute(
            select(AgentNode).where(AgentNode.agent_id.in_(agent_ids))
        )
        rows = result.scalars().all()
    return {n.agent_id: n for n in rows}


async def select_agents(tags: list[str], count: int) -> list[str]:
    """按标签分组选压力机：仅选 ONLINE（空闲），按 CPU 负载升序取前 N 台。

    标签匹配为 OR 语义：节点带有所需标签中的任意一个即命中（分组调度）。
    """
    async with SessionLocal() as db:
        result = await db.execute(
            select(AgentNode)
            .where(AgentNode.status == AgentStatus.ONLINE)
            .order_by(AgentNode.cpu_percent)
        )
        nodes = result.scalars().all()
    wanted = set(tags or [])

    def match(node: AgentNode) -> bool:
        return not wanted or bool(wanted & set(node.tags or []))

    return [n.agent_id for n in nodes if match(n)][:count]
