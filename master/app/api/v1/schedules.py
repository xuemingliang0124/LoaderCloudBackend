"""定时场景管理：创建/启停，联动 APScheduler。"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apscheduler.triggers.cron import CronTrigger

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.schedule import ScheduleJob
from app.schemas import ScheduleIn, ScheduleOut
from app.schemas.common import ok
from app.services import scheduler as scheduler_service
from app.services.exceptions import BusinessError

router = APIRouter()


def _validate_cron(cron: str) -> None:
    try:
        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        raise BusinessError(f"非法 cron 表达式: {exc}", code=4001) from exc


@router.post("/schedules")
async def create_schedule(
    payload: ScheduleIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    _validate_cron(payload.cron)
    job = ScheduleJob(name=payload.name, scenario_id=payload.scenario_id, cron=payload.cron)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    scheduler_service.add_job(job.id, job.scenario_id, job.cron)
    return ok(ScheduleOut.model_validate(job).model_dump(mode="json"))


@router.get("/schedules")
async def list_schedules(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    rows = (await db.execute(select(ScheduleJob).order_by(ScheduleJob.id.desc()))).scalars().all()
    return ok([ScheduleOut.model_validate(r).model_dump(mode="json") for r in rows])


@router.post("/schedules/{job_id}/toggle")
async def toggle_schedule(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    job = await db.get(ScheduleJob, job_id)
    if job is None:
        raise BusinessError("定时任务不存在", code=4002)
    job.enabled = not job.enabled
    await db.commit()
    if job.enabled:
        scheduler_service.add_job(job.id, job.scenario_id, job.cron)
    else:
        scheduler_service.remove_job(job.id)
    return ok(ScheduleOut.model_validate(job).model_dump(mode="json"))
