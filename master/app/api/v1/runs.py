"""场景执行：项目作用域下的手动触发 / 停止 / 记录查询。

执行接口以项目为作用域，统一使用 /projects/{project_id}/runs 嵌套路由：
- 项目不存在返回 3021
- 触发执行：引用场景不存在 3013 / 场景不属于该项目 3022
- 停止执行：记录不存在 2003 / 记录不属于该项目 3022
执行记录的项目归属经 scenario_run.scenario_id → test_scenario.project_id 派生，
场景项目归属不可变，无需冗余列。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.enums import RunTrigger
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.schemas import RunCreateIn, RunOut
from app.schemas.common import ok
from app.services import orchestrator
from app.services.exceptions import BusinessError

router = APIRouter()


async def _get_scoped_run(
    db: AsyncSession, project_id: int, run_no: str
) -> ScenarioRun:
    """按项目作用域取执行记录：不存在 2003，跨项目访问 3022。"""
    run = (
        (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
        .scalars()
        .first()
    )
    if run is None:
        raise BusinessError("执行记录不存在", code=2003)
    scenario = await db.get(Scenario, run.scenario_id)
    if scenario is None or scenario.project_id != project_id:
        raise BusinessError("执行记录不属于指定项目", code=3022)
    return run


@router.post("/projects/{project_id}/runs")
async def create_run(
    payload: RunCreateIn,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """在指定项目下触发执行：场景必须属于该项目（3013/3022）。"""
    await ensure_project_access(db, project_id, user, "editor")
    scenario = await db.get(Scenario, payload.scenario_id)
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)
    if scenario.project_id != project_id:
        raise BusinessError("场景不属于指定项目", code=3022)

    result = await orchestrator.create_run(
        scenario_id=payload.scenario_id,
        trigger=RunTrigger.MANUAL,
        agent_ids=payload.agent_ids,
        created_by=user.username,
    )
    return ok(result)


@router.post("/projects/{project_id}/runs/{run_no}/stop")
async def stop_run(
    run_no: str,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """停止项目内执行：执行记录必须属于该项目（2003/3022）。"""
    await ensure_project_access(db, project_id, user, "editor")
    await _get_scoped_run(db, project_id, run_no)
    await orchestrator.stop_run(run_no)
    return ok(message="停止指令已下发")


@router.get("/projects/{project_id}/runs/{run_no}")
async def get_run(
    run_no: str,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """执行记录详情：返回结构与列表中单条记录一致（2003/3022）。"""
    await ensure_project_access(db, project_id, user, "viewer")
    run = await _get_scoped_run(db, project_id, run_no)
    return ok(RunOut.model_validate(run).model_dump(mode="json"))


@router.get("/projects/{project_id}/runs")
async def list_runs(
    project_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """项目内执行记录分页列表：经场景归属过滤。"""
    await ensure_project_access(db, project_id, user, "viewer")
    total = await db.scalar(
        select(func.count())
        .select_from(ScenarioRun)
        .join(Scenario, ScenarioRun.scenario_id == Scenario.id)
        .where(Scenario.project_id == project_id)
    )
    rows = (
        (
            await db.execute(
                select(ScenarioRun)
                .join(Scenario, ScenarioRun.scenario_id == Scenario.id)
                .where(Scenario.project_id == project_id)
                .order_by(ScenarioRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [RunOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})
