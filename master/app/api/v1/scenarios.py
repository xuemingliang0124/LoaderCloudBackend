"""场景管理：项目作用域 CRUD（创建支持多脚本组合 + 线程组级设置，名称唯一）。

全部场景接口以项目为作用域，统一使用 /projects/{project_id}/scenarios 嵌套路由：
- 项目不存在返回 3021
- 操作具体场景时校验归属，场景不属于该项目返回 3022
- 创建/更新场景时，引用的脚本必须属于同一项目（3022）
- 线程组级 scheduler/duration 不再由接口接收：落库统一 scheduler=True，
  duration 用场景级运行时间覆盖（单交易基准由执行期固定参数另行处理）
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.environment import Environment
from app.models.enums import RunStatus
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.schedule import ScheduleJob
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.models.test_plan import TestPlan
from app.models.test_plan_scenario import TestPlanScenario
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


async def _get_scoped_scenario(
    db: AsyncSession, project_id: int, scenario_id: int
) -> Scenario:
    """按项目作用域取场景：不存在 3013，跨项目访问 3022。

    预加载 scripts 关联：update 场景需遍历旧关联，异步会话下懒加载会
    抛 MissingGreenlet。
    """
    scenario = (
        await db.execute(
            select(Scenario)
            .options(selectinload(Scenario.scripts))
            .where(Scenario.id == scenario_id)
        )
    ).scalar_one_or_none()
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)
    if scenario.project_id != project_id:
        raise BusinessError("场景不属于指定项目", code=3022)
    return scenario


async def _validate_project_scripts(
    db: AsyncSession, project_id: int, script_ids: list[int]
) -> None:
    """校验脚本：场景内不重复（3011）、存在（3012）且属于同一项目（3022）。"""
    if len(script_ids) != len(set(script_ids)):
        raise BusinessError("同一场景内脚本不可重复", code=3011)
    if not script_ids:
        return
    scripts = (
        (await db.execute(select(Script).where(Script.id.in_(script_ids))))
        .scalars()
        .all()
    )
    found_ids = {s.id for s in scripts}
    missing = [sid for sid in script_ids if sid not in found_ids]
    if missing:
        raise BusinessError(f"脚本不存在: {missing}", code=3012)
    foreign = [s.id for s in scripts if s.project_id != project_id]
    if foreign:
        raise BusinessError(f"脚本不属于指定项目: {foreign}", code=3022)


async def _validate_environment_belongs_to_project(
    db: AsyncSession, project_id: int, environment_id: int | None
) -> None:
    """校验绑定环境：传值时必须存在（3041）且属于同一项目（3042）。

    None 表示不绑定环境（兼容存量场景），直接放行。
    """
    if environment_id is None:
        return
    env = (
        await db.execute(select(Environment).where(Environment.id == environment_id))
    ).scalar_one_or_none()
    if env is None:
        raise BusinessError(f"环境不存在: {environment_id}", code=3041)
    if env.project_id != project_id:
        raise BusinessError(
            f"环境不属于指定项目: environment_id={environment_id}", code=3042
        )


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
        project_id=scenario.project_id,
        name=scenario.name,
        scenario_type=scenario.scenario_type,
        duration=scenario.duration,
        environment_id=scenario.environment_id,
        param_overrides=scenario.param_overrides,
        description=scenario.description,
        scripts=scripts_out,
    ).model_dump(mode="json")


def _scenario_detail_stmt(scenario_id: int):
    """构造回读场景及脚本/线程组关联的 select 语句（响应用）。"""
    return (
        select(Scenario)
        .options(selectinload(Scenario.scripts).selectinload(ScenarioScript.script))
        .options(
            selectinload(Scenario.scripts).selectinload(ScenarioScript.thread_groups)
        )
        .where(Scenario.id == scenario_id)
    )


@router.post("/projects/{project_id}/scenarios")
async def create_scenario(
    project_id: int,
    payload: ScenarioIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """在指定项目下创建场景：名称不可重复，支持多脚本组合（脚本须属于该项目）。"""
    await ensure_project_access(db, project_id, user, "editor")

    # 名称唯一校验
    exists = (
        await db.execute(select(Scenario).where(Scenario.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise BusinessError(f"场景名称已存在: {payload.name}", code=3010)

    # 校验脚本：去重 + 存在 + 同项目归属
    script_ids = [s.script_id for s in payload.scripts]
    await _validate_project_scripts(db, project_id, script_ids)

    # 校验绑定环境（可选）：传值时必须属于同一项目
    await _validate_environment_belongs_to_project(
        db, project_id, payload.environment_id
    )

    scenario = Scenario(
        project_id=project_id,
        name=payload.name,
        scenario_type=payload.scenario_type,
        duration=payload.duration,
        environment_id=payload.environment_id,
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
            # 调度器统一开启，运行时长由场景级 duration 覆盖线程组级设置
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.thread_group_name,
                    testclass=tg.testclass,
                    enabled=tg.enabled,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    tps=tg.tps,
                    scheduler=True,
                    duration=payload.duration,
                )
            )

    await db.commit()
    # 回读关联以构造完整响应
    scenario = (await db.execute(_scenario_detail_stmt(scenario.id))).scalar_one()
    return ok(_build_scenario_out(scenario))


@router.get("/projects/{project_id}/scenarios")
async def list_scenarios(
    project_id: int,
    name: str | None = Query(default=None, description="按场景名模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """项目内场景分页列表：仅返回归属该项目的场景，支持名称模糊查询。"""
    await ensure_project_access(db, project_id, user, "viewer")
    filters = [Scenario.project_id == project_id]
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


@router.get("/projects/{project_id}/scenarios/{scenario_id}")
async def get_scenario(
    project_id: int,
    scenario_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """场景详情：基础信息 + 关联脚本（名称/选机标签/数量）及线程组级加压参数。"""
    await ensure_project_access(db, project_id, user, "viewer")
    scenario = (
        await db.execute(_scenario_detail_stmt(scenario_id))
    ).scalar_one_or_none()
    if scenario is None:
        raise BusinessError("场景不存在", code=3013)
    if scenario.project_id != project_id:
        raise BusinessError("场景不属于指定项目", code=3022)
    return ok(_build_scenario_out(scenario))


@router.put("/projects/{project_id}/scenarios/{scenario_id}")
async def update_scenario(
    project_id: int,
    scenario_id: int,
    payload: ScenarioUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新场景：基础信息 + 关联脚本（全量替换，含线程组设置；脚本须属于该项目）。"""
    await ensure_project_access(db, project_id, user, "editor")
    scenario = await _get_scoped_scenario(db, project_id, scenario_id)

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

    # 脚本校验：去重 + 存在 + 同项目归属
    script_ids = [s.script_id for s in payload.scripts]
    await _validate_project_scripts(db, project_id, script_ids)

    # 校验绑定环境（可选）：传值时必须属于同一项目；传 null 表示解绑
    await _validate_environment_belongs_to_project(
        db, project_id, payload.environment_id
    )

    # ---- 更新场景基础信息 ----
    scenario.name = payload.name
    scenario.scenario_type = payload.scenario_type
    scenario.duration = payload.duration
    scenario.environment_id = payload.environment_id
    scenario.param_overrides = payload.param_overrides
    scenario.description = payload.description

    # ---- 全量替换脚本关联 ----
    # 关系集合层面清空：delete-orphan 级联删除旧 scenario_script 及其
    # thread_groups（直接 db.delete 绕过关系集合会让回读命中身份映射旧对象）
    scenario.scripts.clear()
    await db.flush()

    # 重建关联（append 同步关系集合，保证回读响应与 DB 一致）
    for idx, s in enumerate(payload.scripts):
        ss = ScenarioScript(
            scenario_id=scenario.id,
            script_id=s.script_id,
            order_index=s.order_index if s.order_index else idx,
            agent_tags=s.agent_tags,
            agent_count=s.agent_count,
        )
        scenario.scripts.append(ss)
        await db.flush()
        for tg in s.thread_groups:
            # 调度器统一开启，运行时长由场景级 duration 覆盖线程组级设置
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.thread_group_name,
                    testclass=tg.testclass,
                    enabled=tg.enabled,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    tps=tg.tps,
                    scheduler=True,
                    duration=payload.duration,
                )
            )

    await db.commit()
    # 回读关联构造响应
    scenario = (await db.execute(_scenario_detail_stmt(scenario.id))).scalar_one()
    return ok(_build_scenario_out(scenario))


@router.get("/projects/{project_id}/scenarios/{scenario_id}/delete-precheck")
async def precheck_scenario_delete(
    project_id: int,
    scenario_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除前预检：返回运行中任务数、历史执行记录数、引用的定时任务列表。

    前端据此决定是否弹出确认框并发起 force=true 的强制删除；
    running_runs > 0 时无论是否 force 都不可删除（需先停止执行）。
    """
    await ensure_project_access(db, project_id, user, "viewer")
    await _get_scoped_scenario(db, project_id, scenario_id)

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
        await db.execute(
            select(ScheduleJob.id, ScheduleJob.name).where(
                ScheduleJob.scenario_id == scenario_id
            )
        )
    ).all()
    # A4：场景被测试方案挂载的引用统计（预检展示，严格删除 3017 阻断）
    plan_refs = (
        await db.execute(
            select(TestPlan.id, TestPlan.name)
            .join(TestPlanScenario, TestPlanScenario.plan_id == TestPlan.id)
            .where(TestPlanScenario.scenario_id == scenario_id)
        )
    ).all()
    return ok(
        {
            "scenario_id": scenario_id,
            "running_runs": int(running_runs),
            "history_runs": int(history_runs),
            "schedule_jobs": [{"id": r.id, "name": r.name} for r in schedules],
            "test_plans": [{"id": r.id, "name": r.name} for r in plan_refs],
        }
    )


@router.delete("/projects/{project_id}/scenarios/{scenario_id}")
async def delete_scenario(
    project_id: int,
    scenario_id: int,
    force: bool = Query(
        False,
        description="强制级联删除：清理历史执行记录/结果分片/定时任务/MinIO 产物",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除场景（Scenario.scripts 级联删除 scenario_script 与 scenario_script_tg）。

    默认严格模式：存在未结束任务/历史执行记录/定时任务引用时拒绝（3014/3016/3015）。
    force=true 强制级联：按 run_agent_result → scenario_run → 定时任务（含
    APScheduler 注销）→ 场景 顺序清理；运行中任务仍拒绝（需先停止执行）。
    MinIO 产物 runs/{run_no}/ 删除失败仅告警，不影响结果。
    """
    await ensure_project_access(db, project_id, user, "editor")
    scenario = await _get_scoped_scenario(db, project_id, scenario_id)

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
    # A4：测试方案挂载引用（弱关联，删除场景不级联删方案，仅解绑关联行）
    plan_ref_rows = (
        await db.execute(
            select(TestPlan.id, TestPlan.name)
            .join(TestPlanScenario, TestPlanScenario.plan_id == TestPlan.id)
            .where(TestPlanScenario.scenario_id == scenario_id)
        )
    ).all()

    run_nos: list[str] = []
    if force:
        # 解绑方案挂载：删除 test_plan_scenario 关联行，方案本身保留
        await db.execute(
            delete(TestPlanScenario).where(
                TestPlanScenario.scenario_id == scenario_id
            )
        )
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
        # A4：方案挂载阻断（弱关联，保持 Scenario 可独立执行的前提是显式解绑）
        if plan_ref_rows:
            preview = ", ".join(r.name for r in plan_ref_rows[:5])
            more = " 等" if len(plan_ref_rows) > 5 else ""
            raise BusinessError(
                f"场景已被 {len(plan_ref_rows)} 个测试方案挂载，无法删除；"
                f"请先在方案中移除该场景或携带 force=true 强制删除：{preview}{more}",
                code=3017,
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
            "project_id": project_id,
            "deleted": True,
            "force": force,
            "removed_runs": len(run_nos),
            "removed_schedules": len(schedule_rows) if force else 0,
            "removed_plan_refs": len(plan_ref_rows) if force else 0,
            "removed_artifacts": removed_artifacts,
        }
    )
