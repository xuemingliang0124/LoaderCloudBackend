"""项目管理：项目是脚本/场景等测试资产的顶层分组（名称唯一）。

权限语义（P2）：任何登录用户可建项目，创建者在同一事务内自动成为 owner；
非 admin 用户的项目列表仅返回自己为成员的项目，admin 全量可见。
更新/删除要求 owner+（admin 直通）；删除遵循预检 + force 级联模式，
运行中执行任务（PENDING/RUNNING/STOPPING）即使 force 也拒绝（需先停止）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.asset import Asset
from app.models.enums import ProjectRole, RunStatus
from app.models.environment import Environment
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.scenario import Scenario
from app.models.schedule import ScheduleJob
from app.models.script import Script
from app.models.test_plan import TestPlan
from app.models.test_plan_scenario import TestPlanScenario
from app.models.transaction import Transaction
from app.schemas import ProjectIn, ProjectOut, ProjectUpdateIn
from app.schemas.common import like_pattern, ok
from app.services import scheduler as scheduler_service
from app.services import storage as storage_service
from app.services.exceptions import BusinessError

router = APIRouter()

_RUNNING_STATUSES = [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING]


def _role_cn(role: str) -> str:
    """DB 英文角色 → API 中文角色名（与成员管理口径一致）。"""
    return ProjectRole[role.upper()].value


def _project_out(project: Project, my_role: str) -> dict:
    """构造项目响应：附当前请求者的项目内角色（中文）。"""
    out = ProjectOut.model_validate(project).model_dump(mode="json")
    out["my_role"] = my_role
    return out


@router.post("/projects")
async def create_project(
    payload: ProjectIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """新建项目：项目名称不可重复（3020），创建者自动成为 owner。"""
    # name 已由 schema 校验去除首尾空白
    exists = (
        await db.execute(select(Project.id).where(Project.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise BusinessError(f"项目名称已存在: {payload.name}", code=3020)

    project = Project(
        name=payload.name,
        description=payload.description,
        created_by=user.username,
    )
    db.add(project)
    await db.flush()  # 先拿 id，同事务写入 owner 成员，保证「建项目必有 owner」原子性
    db.add(
        ProjectMember(
            project_id=project.id,
            username=user.username,
            role="owner",
            granted_by=user.username,
        )
    )
    await db.commit()
    await db.refresh(project)
    # 创建者同事务写入 owner 成员行（admin 建项目亦然），my_role 恒为项目管理员
    return ok(_project_out(project, ProjectRole.OWNER.value))


@router.get("/projects")
async def list_projects(
    name: str | None = Query(default=None, description="按项目名模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """项目分页列表：支持按名称模糊查询，按 id 倒序，响应含 total。

    非 admin 用户仅可见自己为成员的项目；admin 全量可见。
    """
    filters = []
    if user.role != "admin":
        member_project_ids = select(ProjectMember.project_id).where(
            ProjectMember.username == user.username
        )
        filters.append(Project.id.in_(member_project_ids))
    if name:
        filters.append(Project.name.like(like_pattern(name.strip()), escape="\\"))

    total = await db.scalar(select(func.count()).select_from(Project).where(*filters))
    rows = (
        (
            await db.execute(
                select(Project)
                .where(*filters)
                .order_by(Project.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    # my_role：admin 对所有项目具备管理员级能力，恒出「项目管理员」；
    # 非 admin 用户列表已被成员关系过滤，按成员行映射中文角色
    my_roles: dict[int, str] = {}
    if user.role == "admin":
        my_roles = {r.id: ProjectRole.OWNER.value for r in rows}
    elif rows:
        member_rows = (
            await db.execute(
                select(ProjectMember.project_id, ProjectMember.role).where(
                    ProjectMember.username == user.username,
                    ProjectMember.project_id.in_([r.id for r in rows]),
                )
            )
        ).all()
        my_roles = {row.project_id: _role_cn(row.role) for row in member_rows}
    items = [
        _project_out(r, my_roles.get(r.id, ProjectRole.VIEWER.value)) for r in rows
    ]
    return ok({"total": int(total or 0), "items": items})


@router.put("/projects/{project_id}")
async def update_project(
    project_id: int,
    payload: ProjectUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新项目名称/描述（owner+）：名称不可与其他项目重复（3020）。"""
    await ensure_project_access(db, project_id, user, "owner")
    project = await db.get(Project, project_id)

    if payload.name is not None:
        dup = (
            await db.execute(
                select(Project.id).where(
                    Project.name == payload.name, Project.id != project_id
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise BusinessError(f"项目名称已存在: {payload.name}", code=3020)
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description

    await db.commit()
    await db.refresh(project)
    # 更新门禁已要求 owner+（admin 直通），my_role 恒为项目管理员
    return ok(_project_out(project, ProjectRole.OWNER.value))


@router.get("/projects/{project_id}/delete-precheck")
async def precheck_project_delete(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除前预检：返回项目内脚本数、环境数、交易数、场景数、运行中任务数、引用的定时任务列表。

    前端据此决定是否弹出确认框并发起 force=true 的强制删除；
    running_runs > 0 时无论是否 force 都不可删除（需先停止执行）。
    """
    await ensure_project_access(db, project_id, user, "viewer")

    scenario_ids = (
        (await db.execute(select(Scenario.id).where(Scenario.project_id == project_id)))
        .scalars()
        .all()
    )
    scripts = (
        await db.scalar(
            select(func.count())
            .select_from(Script)
            .where(Script.project_id == project_id)
        )
    ) or 0
    environments = (
        await db.scalar(
            select(func.count())
            .select_from(Environment)
            .where(Environment.project_id == project_id)
        )
    ) or 0
    transactions = (
        await db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.project_id == project_id)
        )
    ) or 0
    assets = (
        await db.scalar(
            select(func.count())
            .select_from(Asset)
            .where(Asset.project_id == project_id)
        )
    ) or 0
    test_plans = (
        await db.scalar(
            select(func.count())
            .select_from(TestPlan)
            .where(TestPlan.project_id == project_id)
        )
    ) or 0
    running_runs = 0
    schedule_jobs: list = []
    if scenario_ids:
        running_runs = (
            await db.scalar(
                select(func.count())
                .select_from(ScenarioRun)
                .where(
                    ScenarioRun.scenario_id.in_(scenario_ids),
                    ScenarioRun.status.in_(_RUNNING_STATUSES),
                )
            )
        ) or 0
        schedule_jobs = (
            await db.execute(
                select(ScheduleJob.id, ScheduleJob.name).where(
                    ScheduleJob.scenario_id.in_(scenario_ids)
                )
            )
        ).all()
    return ok(
        {
            "project_id": project_id,
            "scripts": int(scripts),
            "environments": int(environments),
            "transactions": int(transactions),
            "assets": int(assets),
            "test_plans": int(test_plans),
            "scenarios": len(scenario_ids),
            "running_runs": int(running_runs),
            "schedule_jobs": [{"id": r.id, "name": r.name} for r in schedule_jobs],
        }
    )


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    force: bool = Query(
        False,
        description="强制级联删除：清理项目内场景/执行记录/定时任务/脚本及 MinIO 产物",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除项目（owner+）：遵循「预检 + force 级联」模式。

    默认严格模式：项目内存在脚本、环境、交易或场景时拒绝（3023），需先清理资产或 force。
    force=true 强制级联：按 run_agent_result → scenario_run → 定时任务（含
    APScheduler 注销）→ 场景（级联 scenario_script/scenario_script_tg）→ 脚本 →
    环境 → 交易 → 项目（含成员关系）顺序清理；运行中任务仍拒绝（3014，需先停止执行）。
    MinIO 产物（scripts/{id}/、runs/{run_no}/）在提交后 best-effort 清理，失败仅告警。
    """
    await ensure_project_access(db, project_id, user, "owner")

    scenario_ids = (
        (await db.execute(select(Scenario.id).where(Scenario.project_id == project_id)))
        .scalars()
        .all()
    )
    script_rows = (
        (await db.execute(select(Script).where(Script.project_id == project_id)))
        .scalars()
        .all()
    )
    asset_rows = (
        (await db.execute(select(Asset).where(Asset.project_id == project_id)))
        .scalars()
        .all()
    )
    env_count = (
        await db.scalar(
            select(func.count())
            .select_from(Environment)
            .where(Environment.project_id == project_id)
        )
    ) or 0
    txn_count = (
        await db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.project_id == project_id)
        )
    ) or 0
    asset_count = len(asset_rows)
    plan_count = (
        await db.scalar(
            select(func.count())
            .select_from(TestPlan)
            .where(TestPlan.project_id == project_id)
        )
    ) or 0

    # 未结束的执行任务：force 也不例外，必须先停止（与场景删除口径一致 3014）
    if scenario_ids:
        running_runs = (
            await db.scalar(
                select(func.count())
                .select_from(ScenarioRun)
                .where(
                    ScenarioRun.scenario_id.in_(scenario_ids),
                    ScenarioRun.status.in_(_RUNNING_STATUSES),
                )
            )
        ) or 0
        if running_runs:
            raise BusinessError(
                f"项目下存在 {int(running_runs)} 个未结束的执行任务，无法删除；请先停止执行",
                code=3014,
            )

    if not force and (
        script_rows or scenario_ids or env_count or txn_count or asset_count or plan_count
    ):
        raise BusinessError(
            f"项目下存在 {len(script_rows)} 个脚本、{env_count} 个环境、"
            f"{txn_count} 个交易、{asset_count} 个文档资产、{plan_count} 个测试方案、"
            f"{len(scenario_ids)} 个场景，无法删除；"
            "请先通过删除预检接口确认后携带 force=true 强制删除",
            code=3023,
        )

    run_nos: list[str] = []
    schedule_rows: list[ScheduleJob] = []
    if force:
        # 测试方案：先 bulk delete 挂载关联行（其 scenario_id/plan_id FK 均为
        # RESTRICT，必须先于场景/方案删除解绑），再删方案行解除 test_project FK
        plan_ids = (
            (await db.execute(select(TestPlan.id).where(TestPlan.project_id == project_id)))
            .scalars()
            .all()
        )
        if plan_ids:
            await db.execute(
                delete(TestPlanScenario).where(TestPlanScenario.plan_id.in_(plan_ids))
            )
            await db.execute(delete(TestPlan).where(TestPlan.project_id == project_id))
        if scenario_ids:
            run_nos = (
                (
                    await db.execute(
                        select(ScenarioRun.run_no).where(
                            ScenarioRun.scenario_id.in_(scenario_ids)
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
                delete(ScenarioRun).where(ScenarioRun.scenario_id.in_(scenario_ids))
            )
            # 定时任务：注销 APScheduler + 删业务行
            schedule_rows = (
                (
                    await db.execute(
                        select(ScheduleJob).where(
                            ScheduleJob.scenario_id.in_(scenario_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for job in schedule_rows:
                scheduler_service.remove_job(job.id)
            if schedule_rows:
                await db.execute(
                    delete(ScheduleJob).where(ScheduleJob.scenario_id.in_(scenario_ids))
                )
            # 场景：ORM 级联删除 scenario_script / scenario_script_tg
            scenarios = (
                (
                    await db.execute(
                        select(Scenario).where(Scenario.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            for scenario in scenarios:
                await db.delete(scenario)

        # 脚本行（场景已删，scenario_script 引用已随级联清除）
        for script in script_rows:
            await db.delete(script)
        # 环境行：无 ORM 级联依赖，bulk delete 解除 test_project RESTRICT 外键
        await db.execute(
            delete(Environment).where(Environment.project_id == project_id)
        )
        # 交易行：default_script_id 弱关联已随脚本删除由 DB ondelete 置空，
        # 此处 bulk delete 解除 test_project RESTRICT 外键
        await db.execute(
            delete(Transaction).where(Transaction.project_id == project_id)
        )
        # 文档资产行：bulk delete 解除 test_project RESTRICT 外键
        await db.execute(delete(Asset).where(Asset.project_id == project_id))

    # 项目 + 成员关系（成员 FK 虽为 CASCADE，显式删除保证各库行为一致）
    await db.execute(
        delete(ProjectMember).where(ProjectMember.project_id == project_id)
    )
    project = await db.get(Project, project_id)
    await db.delete(project)
    await db.commit()

    # MinIO 产物清理：事务提交后 best-effort，失败仅告警（delete_prefix 内部已兜底）
    removed_artifacts = 0
    for script in script_rows:
        removed_artifacts += await storage_service.delete_prefix(
            f"scripts/{script.id}/"
        )
    for run_no in run_nos:
        removed_artifacts += await storage_service.delete_prefix(f"runs/{run_no}/")
    for asset in asset_rows:
        if asset.file_key:
            try:
                await storage_service.delete_object(asset.file_key)
                removed_artifacts += 1
            except Exception:  # noqa: BLE001
                from loguru import logger

                logger.warning(
                    f"项目 {project_id} 资产 MinIO 对象删除失败: {asset.file_key}"
                )

    return ok(
        {
            "id": project_id,
            "deleted": True,
            "force": force,
            "removed_scripts": len(script_rows) if force else 0,
            "removed_environments": int(env_count) if force else 0,
            "removed_transactions": int(txn_count) if force else 0,
            "removed_assets": int(asset_count) if force else 0,
            "removed_test_plans": int(plan_count) if force else 0,
            "removed_scenarios": len(scenario_ids) if force else 0,
            "removed_runs": len(run_nos),
            "removed_schedules": len(schedule_rows) if force else 0,
            "removed_artifacts": removed_artifacts,
        }
    )
