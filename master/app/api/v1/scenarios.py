"""场景管理：CRUD（骨架含创建/列表，更新删除 P1 补齐）。"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.scenario import Scenario
from app.schemas import ScenarioIn, ScenarioOut
from app.schemas.common import ok

router = APIRouter()


@router.post("/scenarios")
async def create_scenario(
    payload: ScenarioIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    scenario = Scenario(**payload.model_dump())
    db.add(scenario)
    await db.commit()
    await db.refresh(scenario)
    return ok(ScenarioOut.model_validate(scenario).model_dump(mode="json"))


@router.get("/scenarios")
async def list_scenarios(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    rows = (await db.execute(select(Scenario).order_by(Scenario.id.desc()))).scalars().all()
    return ok([ScenarioOut.model_validate(r).model_dump(mode="json") for r in rows])
