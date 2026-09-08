"""Agent 注册中心：注册、心跳、超时判定、选机调度。"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.agent_node import AgentNode
from app.models.enums import AgentStatus


async def upsert_agent(
    agent_id: str,
    ip: str = "",
    hostname: str = "",
    tags: list[str] | None = None,
    jmeter_version: str = "",
) -> None:
    """Agent 注册/上线刷新（WS 握手时调用）。"""
    async with SessionLocal() as db:
        node = (await db.execute(select(AgentNode).where(AgentNode.agent_id == agent_id))).scalars().first()
        if node is None:
            db.add(
                AgentNode(
                    agent_id=agent_id,
                    ip=ip,
                    hostname=hostname,
                    tags=tags or [],
                    jmeter_version=jmeter_version,
                    status=AgentStatus.ONLINE,
                    last_heartbeat=datetime.now(),
                )
            )
        else:
            node.ip = ip or node.ip
            node.hostname = hostname or node.hostname
            node.tags = tags or node.tags
            node.jmeter_version = jmeter_version or node.jmeter_version
            node.status = AgentStatus.ONLINE
            node.last_heartbeat = datetime.now()
        await db.commit()


async def touch_heartbeat(
    agent_id: str, cpu: float = 0.0, mem: float = 0.0, current_run_no: str | None = None
) -> None:
    """心跳落库：按是否在跑任务区分 ONLINE / BUSY。"""
    async with SessionLocal() as db:
        await db.execute(
            update(AgentNode)
            .where(AgentNode.agent_id == agent_id)
            .values(
                cpu_percent=cpu,
                mem_percent=mem,
                current_run_no=current_run_no,
                status=AgentStatus.BUSY if current_run_no else AgentStatus.ONLINE,
                last_heartbeat=datetime.now(),
            )
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


async def list_agents(db: AsyncSession) -> list[AgentNode]:
    return list((await db.execute(select(AgentNode).order_by(AgentNode.id))).scalars().all())


async def select_agents(tags: list[str], count: int) -> list[str]:
    """按标签选压力机：仅选 ONLINE（空闲），按 CPU 升序取前 N 台。"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(AgentNode)
            .where(AgentNode.status == AgentStatus.ONLINE)
            .order_by(AgentNode.cpu_percent)
        )
        nodes = result.scalars().all()
    wanted = set(tags or [])

    def match(node: AgentNode) -> bool:
        return not wanted or wanted & set(node.tags or [])

    return [n.agent_id for n in nodes if match(n)][:count]
