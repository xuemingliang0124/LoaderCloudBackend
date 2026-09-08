"""压力机管理：列表查询。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.schemas import AgentOut
from app.schemas.common import ok
from app.services import agent_registry

router = APIRouter()


@router.get("/agents")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    nodes = await agent_registry.list_agents(db)
    return ok([AgentOut.model_validate(n).model_dump(mode="json") for n in nodes])
