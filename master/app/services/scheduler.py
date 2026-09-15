"""定时场景调度：APScheduler(AsyncIOScheduler) + MySQL 持久化 jobstore。

到点动作只是"触发下发"（毫秒级 async 调用），执行负载由 Agent 承担，
因此不引入 Celery（决策记录见 docs/tech-selection.md 第 3 节）。
多副本部署时需保证只有一个实例启动调度器（见 skill 文档）。
"""

from sqlalchemy import select, update

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import RunTrigger
from app.models.schedule import ScheduleJob

_scheduler: AsyncIOScheduler | None = None


def job_id(job_pk: int) -> str:
    return f"schedule-{job_pk}"


async def start_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("调度器未启用（SCHEDULER_ENABLED=false）")
        return
    _scheduler = AsyncIOScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(
                url=settings.mysql_dsn_sync, tablename="apscheduler_jobs"
            )
        },
        timezone=settings.timezone,
    )
    _scheduler.start()
    # jobstore 中的任务已随 start() 恢复；再从业务表幂等补偿同步一次
    await _sync_enabled_jobs()
    logger.info("APScheduler 已启动")


async def _sync_enabled_jobs() -> None:
    assert _scheduler is not None
    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(ScheduleJob).where(ScheduleJob.enabled.is_(True))))
            .scalars()
            .all()
        )
    for row in rows:
        add_job(row.id, row.scenario_id, row.cron)


def add_job(job_pk: int, scenario_id: int, cron: str) -> None:
    """注册/更新定时任务（幂等 replace）。调度器未启动时仅告警，下次启动自动补偿。"""
    if _scheduler is None:
        logger.warning(f"调度器未启动，定时任务 {job_pk} 将在启动时同步")
        return
    _scheduler.add_job(
        launch_scheduled_run,
        trigger=CronTrigger.from_crontab(cron, timezone=get_settings().timezone),
        args=[scenario_id, job_pk],
        id=job_id(job_pk),
        replace_existing=True,
        misfire_grace_time=60,
        coalesce=True,
    )


def remove_job(job_pk: int) -> None:
    if _scheduler is not None and _scheduler.get_job(job_id(job_pk)):
        _scheduler.remove_job(job_id(job_pk))


async def launch_scheduled_run(scenario_id: int, job_pk: int) -> None:
    """定时触发入口：只做下发，不承载执行。"""
    from app.services.orchestrator import create_run  # 延迟 import 防循环依赖

    try:
        result = await create_run(
            scenario_id=scenario_id,
            trigger=RunTrigger.SCHEDULED,
            created_by="scheduler",
        )
        async with SessionLocal() as db:
            await db.execute(
                update(ScheduleJob)
                .where(ScheduleJob.id == job_pk)
                .values(last_run_no=result["run_no"])
            )
            await db.commit()
        logger.info(f"定时任务 {job_pk} 已触发 run={result['run_no']}")
    except Exception:  # noqa: BLE001
        logger.exception(f"定时任务 {job_pk} 触发失败（scenario_id={scenario_id}）")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_active_job_count() -> int:
    """当前注册到 APScheduler 的活跃任务数（供 metrics 抓取）。"""
    if _scheduler is None:
        return 0
    try:
        return len(_scheduler.get_jobs())
    except Exception:  # noqa: BLE001
        return 0
