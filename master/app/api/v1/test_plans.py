"""测试方案管理：项目内场景编排单元 CRUD + 删除预检 + 一键批量执行（A4）。

全部方案接口以项目为作用域，统一使用 /projects/{project_id}/test-plans 嵌套路由：
- 项目门禁统一走 ensure_project_access：viewer+ 查询、editor+ 增改/执行、owner+ 删除
- 项目内 name 唯一，重复返回 3070
- 操作具体方案时校验归属：不存在 3071，不属于该项目 3072
- 挂载场景校验：场景不存在/跨项目 3073；方案内重复挂载 3074
- 场景挂载为弱关联（test_plan_scenario）：方案删除仅清理关联行，不触碰场景；
  反向场景删除由场景侧预检阻断（严格 3017，force 解绑），保持 Scenario 可独立执行
- 删除遵循「预检 + force」模式（与环境/交易/资产口径一致）：
  后续定时任务等引用方案后，被引用方案严格模式拒绝（3075），当前阶段恒可删除
- 一键批量执行（execute）：按 seq 升序逐个触发 orchestrator.create_run，
  单场景失败不阻断其余场景，返回逐场景明细（run_no / error）
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.enums import RunTrigger
from app.models.scenario import Scenario
from app.models.test_plan import TestPlan
from app.models.test_plan_scenario import TestPlanScenario
from app.schemas import (
    TestPlanIn,
    TestPlanOut,
    TestPlanScenarioOut,
    TestPlanUpdateIn,
)
from app.schemas.common import like_pattern, ok
from app.services import orchestrator
from app.services.exceptions import BusinessError

router = APIRouter()


def _plan_detail_stmt(plan_id: int):
    """方案详情查询：预加载挂载场景及场景名（async 禁懒加载，必须 selectinload）。"""
    return (
        select(TestPlan)
        .options(
            selectinload(TestPlan.plan_scenarios).selectinload(
                TestPlanScenario.scenario
            )
        )
        .where(TestPlan.id == plan_id)
    )


def _build_plan_out(plan: TestPlan) -> dict:
    """装配方案响应：scenarios 由关系集合 + 场景名构造（非 from_attributes 直出）。"""
    out = TestPlanOut.model_validate(plan)
    out.scenarios = [
        TestPlanScenarioOut(
            scenario_id=ps.scenario_id,
            scenario_name=ps.scenario.name if ps.scenario is not None else "",
            seq=ps.seq,
            weight=ps.weight,
        )
        for ps in plan.plan_scenarios
    ]
    return out.model_dump(mode="json")


async def _get_scoped_plan(
    db: AsyncSession, project_id: int, plan_id: int
) -> TestPlan:
    """按项目作用域取方案：不存在 3071，跨项目访问 3072。"""
    plan = (
        await db.execute(select(TestPlan).where(TestPlan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise BusinessError("测试方案不存在", code=3071)
    if plan.project_id != project_id:
        raise BusinessError("测试方案不属于指定项目", code=3072)
    return plan


async def _check_plan_name_available(
    db: AsyncSession, project_id: int, name: str, exclude_id: int | None = None
) -> None:
    """项目内方案名称唯一校验：重复 3070（更新时排除自身）。"""
    stmt = select(TestPlan.id).where(
        TestPlan.project_id == project_id, TestPlan.name == name
    )
    if exclude_id is not None:
        stmt = stmt.where(TestPlan.id != exclude_id)
    dup = (await db.execute(stmt)).scalar_one_or_none()
    if dup is not None:
        raise BusinessError(f"项目内方案名称已存在: {name}", code=3070)


async def _check_scenarios_scope(
    db: AsyncSession, project_id: int, items: list
) -> None:
    """校验挂载场景：方案内不可重复（3074）；场景必须存在且属于同一项目（3073）。"""
    seen: set[int] = set()
    for item in items:
        if item.scenario_id in seen:
            raise BusinessError(
                f"方案内场景重复挂载: {item.scenario_id}", code=3074
            )
        seen.add(item.scenario_id)
    if not seen:
        return
    rows = (
        await db.execute(
            select(Scenario.id, Scenario.project_id).where(Scenario.id.in_(seen))
        )
    ).all()
    found = {r.id: r.project_id for r in rows}
    for sid in seen:
        if sid not in found:
            raise BusinessError(f"挂载场景不存在: {sid}", code=3073)
        if found[sid] != project_id:
            raise BusinessError(
                f"挂载场景不属于指定项目，不可跨项目挂载: {sid}", code=3073
            )


@router.post("/projects/{project_id}/test-plans")
async def create_test_plan(
    project_id: int,
    payload: TestPlanIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """新建测试方案（editor+）：项目内 name 不可重复（3070），scenarios 一并挂载。"""
    await ensure_project_access(db, project_id, user, "editor")
    await _check_plan_name_available(db, project_id, payload.name)
    await _check_scenarios_scope(db, project_id, payload.scenarios)

    plan = TestPlan(
        project_id=project_id,
        name=payload.name,
        pass_criteria=payload.pass_criteria,
        report_template=payload.report_template,
        description=payload.description,
    )
    db.add(plan)
    await db.flush()
    for item in payload.scenarios:
        db.add(
            TestPlanScenario(
                plan_id=plan.id,
                scenario_id=item.scenario_id,
                seq=item.seq,
                weight=item.weight,
            )
        )
    await db.commit()

    plan = (await db.execute(_plan_detail_stmt(plan.id))).scalar_one()
    return ok(_build_plan_out(plan))


@router.get("/projects/{project_id}/test-plans")
async def list_test_plans(
    project_id: int,
    name: str | None = Query(default=None, description="按方案名称模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """方案分页列表（viewer+）：支持名称模糊过滤，按 id 倒序，响应含 total。"""
    await ensure_project_access(db, project_id, user, "viewer")

    filters = [TestPlan.project_id == project_id]
    if name:
        filters.append(TestPlan.name.like(like_pattern(name.strip()), escape="\\"))

    total = await db.scalar(
        select(func.count()).select_from(TestPlan).where(*filters)
    )
    rows = (
        (
            await db.execute(
                select(TestPlan)
                .options(
                    selectinload(TestPlan.plan_scenarios).selectinload(
                        TestPlanScenario.scenario
                    )
                )
                .where(*filters)
                .order_by(TestPlan.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [_build_plan_out(r) for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.get("/projects/{project_id}/test-plans/{plan_id}")
async def get_test_plan(
    project_id: int,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """方案详情（viewer+）：不存在 3071，跨项目 3072；含挂载场景列表。"""
    await ensure_project_access(db, project_id, user, "viewer")
    await _get_scoped_plan(db, project_id, plan_id)
    plan = (await db.execute(_plan_detail_stmt(plan_id))).scalar_one()
    return ok(_build_plan_out(plan))


@router.put("/projects/{project_id}/test-plans/{plan_id}")
async def update_test_plan(
    project_id: int,
    plan_id: int,
    payload: TestPlanUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新方案（editor+）：name 变更后不可与项目内其他方案重复（3070）。

    scenarios 传则全量替换挂载集合（与场景更新脚本关联口径一致）：
    关系集合 clear() + append()，避免回读命中身份映射旧对象。
    """
    await ensure_project_access(db, project_id, user, "editor")
    plan = (
        await db.execute(_plan_detail_stmt(plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise BusinessError("测试方案不存在", code=3071)
    if plan.project_id != project_id:
        raise BusinessError("测试方案不属于指定项目", code=3072)

    if payload.name is not None and payload.name != plan.name:
        await _check_plan_name_available(db, project_id, payload.name, exclude_id=plan_id)

    if payload.name is not None:
        plan.name = payload.name
    if payload.pass_criteria is not None:
        plan.pass_criteria = payload.pass_criteria
    if payload.report_template is not None:
        plan.report_template = payload.report_template
    if payload.description is not None:
        plan.description = payload.description

    if payload.scenarios is not None:
        await _check_scenarios_scope(db, project_id, payload.scenarios)
        # 关系集合层面清空：delete-orphan 级联删除旧关联行
        plan.plan_scenarios.clear()
        await db.flush()
        for item in payload.scenarios:
            plan.plan_scenarios.append(
                TestPlanScenario(
                    plan_id=plan.id,
                    scenario_id=item.scenario_id,
                    seq=item.seq,
                    weight=item.weight,
                )
            )
        await db.flush()

    await db.commit()
    plan = (await db.execute(_plan_detail_stmt(plan_id))).scalar_one()
    return ok(_build_plan_out(plan))


@router.get("/projects/{project_id}/test-plans/{plan_id}/delete-precheck")
async def precheck_test_plan_delete(
    project_id: int,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除前预检（viewer+）：返回挂载场景数与外部引用数。

    references 为前向兼容字段（如方案级定时任务引用），当前阶段恒为 0。
    """
    await ensure_project_access(db, project_id, user, "viewer")
    await _get_scoped_plan(db, project_id, plan_id)
    scenario_count = (
        await db.scalar(
            select(func.count())
            .select_from(TestPlanScenario)
            .where(TestPlanScenario.plan_id == plan_id)
        )
    ) or 0
    return ok(
        {"plan_id": plan_id, "scenarios": int(scenario_count), "references": 0}
    )


@router.delete("/projects/{project_id}/test-plans/{plan_id}")
async def delete_test_plan(
    project_id: int,
    plan_id: int,
    force: bool = Query(
        False,
        description="强制删除：先解除外部引用再删方案（预留，当前无引用方）",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除方案（owner+）：遵循「预检 + force」模式。

    仅删除方案与挂载关联行（ORM delete-orphan 级联），不触碰场景本身。
    后续引用方落地后，严格模式引用数 > 0 抛 3075，force 模式先解绑再删。
    """
    await ensure_project_access(db, project_id, user, "owner")
    plan = (
        await db.execute(_plan_detail_stmt(plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise BusinessError("测试方案不存在", code=3071)
    if plan.project_id != project_id:
        raise BusinessError("测试方案不属于指定项目", code=3072)

    referencing = 0
    if not force and referencing:
        raise BusinessError(
            f"测试方案被 {referencing} 处引用，无法删除；请先解绑引用或携带 force=true 强制删除",
            code=3075,
        )

    removed_scenarios = len(plan.plan_scenarios)
    await db.delete(plan)
    await db.commit()
    return ok(
        {
            "id": plan_id,
            "deleted": True,
            "force": force,
            "removed_plan_scenarios": removed_scenarios,
            "removed_references": referencing if force else 0,
        }
    )


@router.post("/projects/{project_id}/test-plans/{plan_id}/execute")
async def execute_test_plan(
    project_id: int,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """一键批量起场景（editor+）：按 seq 升序逐个触发执行。

    单场景失败（如无可用压力机）不阻断其余场景，返回逐场景明细；
    方案未挂载场景时拒绝（3076）。
    """
    await ensure_project_access(db, project_id, user, "editor")
    await _get_scoped_plan(db, project_id, plan_id)
    plan = (await db.execute(_plan_detail_stmt(plan_id))).scalar_one()
    if not plan.plan_scenarios:
        raise BusinessError("方案未挂载任何场景，无法执行", code=3076)

    results: list[dict] = []
    for ps in plan.plan_scenarios:
        scenario_name = ps.scenario.name if ps.scenario is not None else ""
        try:
            # orchestrator.create_run 内部自开会话，不依赖当前请求 session
            result = await orchestrator.create_run(
                scenario_id=ps.scenario_id,
                trigger=RunTrigger.MANUAL,
                created_by=user.username,
            )
            results.append(
                {
                    "scenario_id": ps.scenario_id,
                    "scenario_name": scenario_name,
                    "ok": True,
                    "run_no": result["run_no"],
                    "error": "",
                }
            )
        except BusinessError as exc:
            results.append(
                {
                    "scenario_id": ps.scenario_id,
                    "scenario_name": scenario_name,
                    "ok": False,
                    "run_no": None,
                    "error": f"[{exc.code}] {exc.message}",
                }
            )

    succeeded = sum(1 for r in results if r["ok"])
    return ok(
        {
            "plan_id": plan_id,
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "runs": results,
        }
    )
