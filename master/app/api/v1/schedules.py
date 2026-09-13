"""定时场景管理：项目作用域下的创建/启停，联动 APScheduler。

定时任务接口以项目为作用域，统一使用 /projects/{project_id}/schedules 嵌套路由：
- 项目不存在返回 3021
- 创建：引用场景不存在 3013 / 场景不属于该项目 3022 / 非法 cron 4001
- 启停：任务不存在 4002 / 任务不属于该项目 3022
定时任务的项目归属经 schedule_job.scenario_id → test_scenario.project_id 派生，
场景项目归属不可变，无需冗余列。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apscheduler.triggers.cron import CronTrigger

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.scenario import Scenario
from app.models.schedule import ScheduleJob
from app.schemas import ScheduleIn, ScheduleOut
from app.schemas.common import like_pattern, ok
from app.services import scheduler as scheduler_service
from app.services.exceptions import BusinessError

router = APIRouter()


def _validate_cron(cron: str) -> None:
    try:
        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        raise BusinessError(f"非法 cron 表达式: {exc}", code=4001) from exc


async def _get_scoped_job(
    db: AsyncSession, project_id: int, job_id: int
) -> ScheduleJob:
    """按项目作用域取定时任务：不存在 4002，跨项目访问 3022。"""
    job = await db.get(ScheduleJob, job_id)
    if job is None:
        raise BusinessError("定时任务不存在", code=4002)
    scenario = await db.get(Scenario, job.scenario_id)
    if scenario is None or scenario.project_id != project_id:
        raise BusinessError("定时任务不属于指定项目", code=3022)
    return job


@router.post("/projects/{project_id}/schedules")
async def create_schedule(
    payload: ScheduleIn,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """在指定项目下创建定时任务：引用场景必须属于该项目（3013/3022）。"""
    await ensure_project_access(db, project_id, user, "editor")
    scenario = await db.get(Scenario, payload.scenario_id)
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)
    if scenario.project_id != project_id:
        raise BusinessError("场景不属于指定项目", code=3022)

    _validate_cron(payload.cron)
    job = ScheduleJob(
        name=payload.name, scenario_id=payload.scenario_id, cron=payload.cron
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    scheduler_service.add_job(job.id, job.scenario_id, job.cron)
    return ok(ScheduleOut.model_validate(job).model_dump(mode="json"))


@router.get("/projects/{project_id}/schedules")
async def list_schedules(
    project_id: int,
    name: str | None = Query(default=None, description="按任务名模糊查询"),
    enabled: bool | None = Query(default=None, description="按启用状态精确过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """项目内定时任务分页列表：经场景归属过滤，支持名称/启用状态过滤。"""
    await ensure_project_access(db, project_id, user, "viewer")
    filters = [Scenario.project_id == project_id]
    if name:
        filters.append(ScheduleJob.name.like(like_pattern(name.strip()), escape="\\"))
    if enabled is not None:
        filters.append(ScheduleJob.enabled == enabled)

    total = await db.scalar(
        select(func.count())
        .select_from(ScheduleJob)
        .join(Scenario, ScheduleJob.scenario_id == Scenario.id)
        .where(*filters)
    )
    rows = (
        (
            await db.execute(
                select(ScheduleJob)
                .join(Scenario, ScheduleJob.scenario_id == Scenario.id)
                .where(*filters)
                .order_by(ScheduleJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [ScheduleOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.post("/projects/{project_id}/schedules/{job_id}/toggle")
async def toggle_schedule(
    job_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """启停项目内定时任务：任务必须属于该项目（4002/3022）。"""
    await ensure_project_access(db, project_id, user, "editor")
    job = await _get_scoped_job(db, project_id, job_id)
    job.enabled = not job.enabled
    await db.commit()
    if job.enabled:
        scheduler_service.add_job(job.id, job.scenario_id, job.cron)
    else:
        scheduler_service.remove_job(job.id)
    return ok(ScheduleOut.model_validate(job).model_dump(mode="json"))
