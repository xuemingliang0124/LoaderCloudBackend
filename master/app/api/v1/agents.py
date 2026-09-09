"""压力机管理：注册（Agent 启动调用）+ 列表查询。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.schemas import AgentOut, AgentRegisterIn, AgentRegisterOut
from app.schemas.common import ok
from app.services import agent_registry
from app.services.exceptions import BusinessError

router = APIRouter()


@router.post("/agents/register")
async def register_agent(payload: AgentRegisterIn) -> dict:
    """Agent 启动注册：按宿主机 IP 换取固定 agent_id（公开端点，Agent 无 JWT）。

    首次上报该 IP 时 Master 自动建号；后续启动返回同一 agent_id。
    """
    if not payload.ip:
        raise BusinessError("Agent 宿主机 IP 为空，无法注册", code=3101)
    node, is_new = await agent_registry.register_by_ip(
        ip=payload.ip,
        hostname=payload.hostname,
        tags=payload.tags,
        jmeter_version=payload.jmeter_version,
    )
    return ok(
        AgentRegisterOut(
            agent_id=node.agent_id,
            ip=node.ip,
            hostname=node.hostname,
            is_new=is_new,
        ).model_dump()
    )


@router.get("/agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    nodes = await agent_registry.list_agents(db)
    return ok([AgentOut.model_validate(n).model_dump(mode="json") for n in nodes])
