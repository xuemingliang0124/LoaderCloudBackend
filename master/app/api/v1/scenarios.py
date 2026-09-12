"""场景管理：CRUD（创建支持多脚本组合 + 线程组级设置，名称唯一）。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.enums import RunStatus
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.schedule import ScheduleJob
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.schemas import (
    ScenarioIn,
    ScenarioOut,
    ScenarioScriptOut,
    ScenarioUpdateIn,
    ThreadGroupSettingOut,
)
from app.schemas.common import like_pattern, ok
from app.services import scheduler as scheduler_service
from app.services import storage as storage_service
from app.services.exceptions import BusinessError

router = APIRouter()


def _build_scenario_out(scenario: Scenario) -> dict:
    """把 Scenario ORM（含 scripts/thread_groups/script 关联）转为响应 dict。"""
    scripts_out: list[dict] = []
    for ss in scenario.scripts:
        script_name = ss.script.name if ss.script is not None else ""
        tgs = [
            ThreadGroupSettingOut.model_validate(tg).model_dump(mode="json")
            for tg in ss.thread_groups
        ]
        scripts_out.append(
            ScenarioScriptOut(
                id=ss.id,
                script_id=ss.script_id,
                order_index=ss.order_index,
                agent_tags=ss.agent_tags,
                agent_count=ss.agent_count,
                script_name=script_name,
                thread_groups=tgs,
            ).model_dump(mode="json")
        )
    return ScenarioOut(
        id=scenario.id,
        name=scenario.name,
        scenario_type=scenario.scenario_type,
        duration=scenario.duration,
        param_overrides=scenario.param_overrides,
        description=scenario.description,
        scripts=scripts_out,
    ).model_dump(mode="json")


@router.post("/scenarios")
async def create_scenario(
    payload: ScenarioIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """创建场景：名称不可重复，支持多脚本组合，每个脚本可配置各线程组加压参数。"""
    # 名称唯一校验
    exists = (
        await db.execute(select(Scenario).where(Scenario.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise BusinessError(f"场景名称已存在: {payload.name}", code=3010)

    # 校验脚本存在性 + 去重（同一场景内同一脚本只允许出现一次）
    script_ids = [s.script_id for s in payload.scripts]
    if len(script_ids) != len(set(script_ids)):
        raise BusinessError("同一场景内脚本不可重复", code=3011)
    if script_ids:
        scripts = (
            (await db.execute(select(Script).where(Script.id.in_(script_ids))))
            .scalars()
            .all()
        )
        found_ids = {s.id for s in scripts}
        missing = [sid for sid in script_ids if sid not in found_ids]
        if missing:
            raise BusinessError(f"脚本不存在: {missing}", code=3012)

    scenario = Scenario(
        name=payload.name,
        scenario_type=payload.scenario_type,
        duration=payload.duration,
        param_overrides=payload.param_overrides,
        description=payload.description,
    )
    db.add(scenario)
    await db.flush()

    for idx, s in enumerate(payload.scripts):
        ss = ScenarioScript(
            scenario_id=scenario.id,
            script_id=s.script_id,
            order_index=s.order_index if s.order_index else idx,
            agent_tags=s.agent_tags,
            agent_count=s.agent_count,
        )
        db.add(ss)
        await db.flush()
        for tg in s.thread_groups:
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.thread_group_name,
                    testclass=tg.testclass,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    loops=tg.loops,
                    scheduler=tg.scheduler,
                    duration=tg.duration,
                )
            )

    await db.commit()
    # 回读关联以构造完整响应
    scenario = (
        await db.execute(
            select(Scenario)
            .options(selectinload(Scenario.scripts).selectinload(ScenarioScript.script))
            .options(
                selectinload(Scenario.scripts).selectinload(
                    ScenarioScript.thread_groups
                )
            )
            .where(Scenario.id == scenario.id)
        )
    ).scalar_one()
    return ok(_build_scenario_out(scenario))


@router.get("/scenarios")
async def list_scenarios(
    name: str | None = Query(default=None, description="按场景名模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    filters = []
    if name:
        filters.append(Scenario.name.like(like_pattern(name.strip()), escape="\\"))

    total = await db.scalar(select(func.count()).select_from(Scenario).where(*filters))
    rows = (
        (
            await db.execute(
                select(Scenario)
                .options(
                    selectinload(Scenario.scripts).selectinload(ScenarioScript.script)
                )
                .options(
                    selectinload(Scenario.scripts).selectinload(
                        ScenarioScript.thread_groups
                    )
                )
                .where(*filters)
                .order_by(Scenario.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [_build_scenario_out(r) for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.put("/scenarios/{scenario_id}")
async def update_scenario(
    scenario_id: int,
    payload: ScenarioUpdateIn,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """更新场景：基础信息 + 关联脚本（全量替换，含线程组设置）。"""
    scenario = (
        await db.execute(select(Scenario).where(Scenario.id == scenario_id))
    ).scalar_one_or_none()
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)

    # 运行中/待执行的场景不允许修改，避免下发配置与落库配置不一致
    running = (
        await db.execute(
            select(ScenarioRun).where(
                ScenarioRun.scenario_id == scenario_id,
                ScenarioRun.status.in_(
                    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING]
                ),
            )
        )
    ).scalar_one_or_none()
    if running is not None:
        raise BusinessError("场景存在未结束的执行任务，无法修改", code=3014)

    # 名称唯一校验（排除自身）
    dup = (
        await db.execute(
            select(Scenario).where(
                Scenario.name == payload.name, Scenario.id != scenario_id
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise BusinessError(f"场景名称已存在: {payload.name}", code=3010)

    # 脚本去重 + 存在性校验
    script_ids = [s.script_id for s in payload.scripts]
    if len(script_ids) != len(set(script_ids)):
        raise BusinessError("同一场景内脚本不可重复", code=3011)
    if script_ids:
        scripts = (
            (await db.execute(select(Script).where(Script.id.in_(script_ids))))
            .scalars()
            .all()
        )
        found_ids = {s.id for s in scripts}
        missing = [sid for sid in script_ids if sid not in found_ids]
        if missing:
            raise BusinessError(f"脚本不存在: {missing}", code=3012)

    # ---- 更新场景基础信息 ----
    scenario.name = payload.name
    scenario.scenario_type = payload.scenario_type
    scenario.duration = payload.duration
    scenario.param_overrides = payload.param_overrides
    scenario.description = payload.description

    # ---- 全量替换脚本关联 ----
    # 删除旧关联：cascade="all, delete-orphan" 会连带删 scenario_script_tg
    for old_ss in list(scenario.scripts):
        db.delete(old_ss)
    await db.flush()

    # 重建关联
    for idx, s in enumerate(payload.scripts):
        ss = ScenarioScript(
            scenario_id=scenario.id,
            script_id=s.script_id,
            order_index=s.order_index if s.order_index else idx,
            agent_tags=s.agent_tags,
            agent_count=s.agent_count,
        )
        db.add(ss)
        await db.flush()
        for tg in s.thread_groups:
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.thread_group_name,
                    testclass=tg.testclass,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    loops=tg.loops,
                    scheduler=tg.scheduler,
                    duration=tg.duration,
                )
            )

    await db.commit()
    # 回读关联构造响应
    scenario = (
        await db.execute(
            select(Scenario)
            .options(selectinload(Scenario.scripts).selectinload(ScenarioScript.script))
            .options(
                selectinload(Scenario.scripts).selectinload(
                    ScenarioScript.thread_groups
                )
            )
            .where(Scenario.id == scenario.id)
        )
    ).scalar_one()
    return ok(_build_scenario_out(scenario))


@router.get("/scenarios/{scenario_id}/delete-precheck")
async def precheck_scenario_delete(
    scenario_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """删除前预检：返回运行中任务数、历史执行记录数、引用的定时任务列表。

    前端据此决定是否弹出确认框并发起 force=true 的强制删除；
    running_runs > 0 时无论是否 force 都不可删除（需先停止执行）。
    """
    scenario = (
        await db.execute(select(Scenario).where(Scenario.id == scenario_id))
    ).scalar_one_or_none()
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)

    running_runs = (
        await db.scalar(
            select(func.count())
            .select_from(ScenarioRun)
            .where(
                ScenarioRun.scenario_id == scenario_id,
                ScenarioRun.status.in_(
                    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING]
                ),
            )
        )
    ) or 0
    history_runs = (
        await db.scalar(
            select(func.count())
            .select_from(ScenarioRun)
            .where(ScenarioRun.scenario_id == scenario_id)
        )
    ) - running_runs
    schedules = (
        (
            await db.execute(
                select(ScheduleJob.id, ScheduleJob.name).where(
                    ScheduleJob.scenario_id == scenario_id
                )
            )
        )
        .all()
    )
    return ok(
        {
            "scenario_id": scenario_id,
            "running_runs": int(running_runs),
            "history_runs": int(history_runs),
            "schedule_jobs": [{"id": r.id, "name": r.name} for r in schedules],
        }
    )


@router.delete("/scenarios/{scenario_id}")
async def delete_scenario(
    scenario_id: int,
    force: bool = Query(
        False,
        description="强制级联删除：清理历史执行记录/结果分片/定时任务/MinIO 产物",
    ),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    """删除场景（Scenario.scripts 级联删除 scenario_script 与 scenario_script_tg）。

    默认严格模式：存在未结束任务/历史执行记录/定时任务引用时拒绝（3014/3016/3015）。
    force=true 强制级联：按 run_agent_result → scenario_run → 定时任务（含
    APScheduler 注销）→ 场景 顺序清理；运行中任务仍拒绝（需先停止执行）。
    MinIO 产物 runs/{run_no}/ 删除失败仅告警，不影响结果。
    """
    scenario = (
        await db.execute(select(Scenario).where(Scenario.id == scenario_id))
    ).scalar_one_or_none()
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)

    # 存在未结束的执行任务时不允许删除（force 也不例外，必须先停止执行）
    running = (
        await db.execute(
            select(ScenarioRun).where(
                ScenarioRun.scenario_id == scenario_id,
                ScenarioRun.status.in_(
                    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING]
                ),
            )
        )
    ).scalar_one_or_none()
    if running is not None:
        raise BusinessError("场景存在未结束的执行任务，无法删除", code=3014)

    schedule_rows = (
        (
            await db.execute(
                select(ScheduleJob).where(ScheduleJob.scenario_id == scenario_id)
            )
        )
        .scalars()
        .all()
    )

    run_nos: list[str] = []
    if force:
        run_nos = (
            (
                await db.execute(
                    select(ScenarioRun.run_no).where(
                        ScenarioRun.scenario_id == scenario_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # 结果分片必须先删：其 scenario_script_id FK 指向 scenario_script（RESTRICT）
        if run_nos:
            await db.execute(
                delete(RunAgentResult).where(RunAgentResult.run_no.in_(run_nos))
            )
            await db.execute(
                delete(ScenarioRun).where(ScenarioRun.scenario_id == scenario_id)
            )
        # 定时任务：注销 APScheduler + 删业务行（无 FK，强制删除时一并清理避免悬空）
        for job in schedule_rows:
            scheduler_service.remove_job(job.id)
            await db.delete(job)
    else:
        # 历史执行记录阻断（修复：此前未拦截导致 MySQL 1451 → 500）
        history_runs = (
            await db.scalar(
                select(func.count())
                .select_from(ScenarioRun)
                .where(ScenarioRun.scenario_id == scenario_id)
            )
        ) or 0
        if history_runs:
            raise BusinessError(
                f"场景存在 {int(history_runs)} 条历史执行记录，无法删除；"
                "请先通过删除预检接口确认后携带 force=true 强制删除",
                code=3016,
            )
        if schedule_rows:
            preview = ", ".join(j.name for j in schedule_rows[:5])
            more = " 等" if len(schedule_rows) > 5 else ""
            raise BusinessError(
                f"场景已被 {len(schedule_rows)} 个定时任务引用，请先删除对应定时任务：{preview}{more}",
                code=3015,
            )

    await db.delete(scenario)
    await db.commit()

    # MinIO 产物清理：事务提交后 best-effort，失败仅告警
    removed_artifacts = 0
    if run_nos:
        for run_no in run_nos:
            removed_artifacts += await storage_service.delete_prefix(f"runs/{run_no}/")

    return ok(
        {
            "id": scenario_id,
            "deleted": True,
            "force": force,
            "removed_runs": len(run_nos),
            "removed_schedules": len(schedule_rows) if force else 0,
            "removed_artifacts": removed_artifacts,
        }
    )
