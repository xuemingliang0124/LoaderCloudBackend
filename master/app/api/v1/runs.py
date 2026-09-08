"""场景执行：手动触发 / 停止 / 记录查询。"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.enums import RunTrigger
from app.models.run import ScenarioRun
from app.schemas import RunCreateIn, RunOut
from app.schemas.common import ok
from app.services import orchestrator

router = APIRouter()


@router.post("/runs")
async def create_run(
    payload: RunCreateIn,
    user: str = Depends(get_current_user),
) -> dict:
    result = await orchestrator.create_run(
        scenario_id=payload.scenario_id,
        trigger=RunTrigger.MANUAL,
        agent_ids=payload.agent_ids,
        created_by=user,
    )
    return ok(result)


@router.post("/runs/{run_no}/stop")
async def stop_run(run_no: str, _: str = Depends(get_current_user)) -> dict:
    await orchestrator.stop_run(run_no)
    return ok(message="停止指令已下发")


@router.get("/runs")
async def list_runs(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    total = await db.scalar(select(func.count()).select_from(ScenarioRun))
    rows = (
        (
            await db.execute(
                select(ScenarioRun)
                .order_by(ScenarioRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [RunOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": total, "items": items})
