"""交易清单管理：项目内被测交易资产 CRUD + 删除预检。

全部交易接口以项目为作用域，统一使用 /projects/{project_id}/transactions 嵌套路由：
- 项目门禁统一走 ensure_project_access：viewer+ 查询、editor+ 增改、owner+ 删除
- 项目内 txn_code 唯一，重复返回 3050
- 操作具体交易时校验归属：不存在 3051，不属于该项目 3052
- 删除遵循「预检 + force」模式（与脚本/场景/环境/项目口径一致）：
  后续场景/方案引用交易（A3/A4）后，被引用的交易严格模式拒绝（3053），
  force=true 先解绑引用再删；当前阶段暂无引用方，严格模式恒可删除。
- default_script_id 为弱关联：脚本删除时由 DB ondelete=SET NULL 自动置空，
  交易创建/更新时仅校验脚本存在且属于同一项目（跨项目脚本不可绑定为默认）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.script import Script
from app.models.transaction import Transaction
from app.schemas import TransactionIn, TransactionOut, TransactionUpdateIn
from app.schemas.common import like_pattern, ok
from app.services.exceptions import BusinessError

router = APIRouter()


async def _get_scoped_transaction(
    db: AsyncSession, project_id: int, txn_id: int
) -> Transaction:
    """按项目作用域取交易：不存在 3051，跨项目访问 3052。"""
    txn = (
        await db.execute(select(Transaction).where(Transaction.id == txn_id))
    ).scalar_one_or_none()
    if txn is None:
        raise BusinessError("交易不存在", code=3051)
    if txn.project_id != project_id:
        raise BusinessError("交易不属于指定项目", code=3052)
    return txn


async def _check_txn_code_available(
    db: AsyncSession, project_id: int, txn_code: str, exclude_id: int | None = None
) -> None:
    """项目内 txn_code 唯一校验：重复 3050（更新时排除自身）。"""
    stmt = select(Transaction.id).where(
        Transaction.project_id == project_id, Transaction.txn_code == txn_code
    )
    if exclude_id is not None:
        stmt = stmt.where(Transaction.id != exclude_id)
    dup = (await db.execute(stmt)).scalar_one_or_none()
    if dup is not None:
        raise BusinessError(f"项目内交易编码已存在: {txn_code}", code=3050)


async def _check_default_script_scope(
    db: AsyncSession, project_id: int, script_id: int | None
) -> None:
    """校验默认脚本归属：脚本必须存在且属于同一项目（跨项目不可绑定为默认）。"""
    if script_id is None:
        return
    script = (
        await db.execute(select(Script).where(Script.id == script_id))
    ).scalar_one_or_none()
    if script is None:
        raise BusinessError(
            f"默认脚本不存在: {script_id}", code=3054
        )
    if script.project_id != project_id:
        raise BusinessError(
            "默认脚本不属于指定项目，不可跨项目绑定", code=3054
        )


@router.post("/projects/{project_id}/transactions")
async def create_transaction(
    project_id: int,
    payload: TransactionIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """新建交易（editor+）：项目内 txn_code 不可重复（3050）。"""
    await ensure_project_access(db, project_id, user, "editor")
    await _check_txn_code_available(db, project_id, payload.txn_code)
    await _check_default_script_scope(db, project_id, payload.default_script_id)

    txn = Transaction(
        project_id=project_id,
        name=payload.name,
        txn_code=payload.txn_code,
        default_script_id=payload.default_script_id,
        sla_tps=payload.sla_tps,
        sla_p95_ms=payload.sla_p95_ms,
        sla_error_rate=payload.sla_error_rate,
        description=payload.description,
    )
    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    return ok(TransactionOut.model_validate(txn).model_dump(mode="json"))


@router.get("/projects/{project_id}/transactions")
async def list_transactions(
    project_id: int,
    name: str | None = Query(default=None, description="按交易名称模糊查询"),
    txn_code: str | None = Query(default=None, description="按交易编码精确查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """交易分页列表（viewer+）：支持名称模糊/编码精确过滤，按 id 倒序，响应含 total。"""
    await ensure_project_access(db, project_id, user, "viewer")

    filters = [Transaction.project_id == project_id]
    if name:
        filters.append(
            Transaction.name.like(like_pattern(name.strip()), escape="\\")
        )
    if txn_code:
        filters.append(Transaction.txn_code == txn_code.strip())

    total = await db.scalar(
        select(func.count()).select_from(Transaction).where(*filters)
    )
    rows = (
        (
            await db.execute(
                select(Transaction)
                .where(*filters)
                .order_by(Transaction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [TransactionOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.get("/projects/{project_id}/transactions/{txn_id}")
async def get_transaction(
    project_id: int,
    txn_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """交易详情（viewer+）：不存在 3051，跨项目 3052。"""
    await ensure_project_access(db, project_id, user, "viewer")
    txn = await _get_scoped_transaction(db, project_id, txn_id)
    return ok(TransactionOut.model_validate(txn).model_dump(mode="json"))


@router.put("/projects/{project_id}/transactions/{txn_id}")
async def update_transaction(
    project_id: int,
    txn_id: int,
    payload: TransactionUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新交易（editor+）：txn_code 变更后不可与项目内其他交易重复（3050）。"""
    await ensure_project_access(db, project_id, user, "editor")
    txn = await _get_scoped_transaction(db, project_id, txn_id)

    if payload.txn_code is not None and payload.txn_code != txn.txn_code:
        await _check_txn_code_available(
            db, project_id, payload.txn_code, exclude_id=txn_id
        )
    # 默认脚本变更：校验新脚本归属（None 表示显式置空，跳过校验）
    if payload.default_script_id is not None:
        await _check_default_script_scope(db, project_id, payload.default_script_id)

    for field in (
        "name",
        "txn_code",
        "default_script_id",
        "sla_tps",
        "sla_p95_ms",
        "sla_error_rate",
        "description",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(txn, field, value)

    await db.commit()
    await db.refresh(txn)
    return ok(TransactionOut.model_validate(txn).model_dump(mode="json"))


@router.get("/projects/{project_id}/transactions/{txn_id}/delete-precheck")
async def precheck_transaction_delete(
    project_id: int,
    txn_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除前预检（viewer+）：返回引用该交易的场景/方案数。

    A3/A4 场景或测试方案引用交易（scenario.transaction_id / test_plan_transaction）后，
    此处统计引用数；当前阶段恒为 0。running_runs 等更细粒度预检随引用方一并补充。
    """
    await ensure_project_access(db, project_id, user, "viewer")
    await _get_scoped_transaction(db, project_id, txn_id)
    return ok({"transaction_id": txn_id, "scenarios": 0, "test_plans": 0})


@router.delete("/projects/{project_id}/transactions/{txn_id}")
async def delete_transaction(
    project_id: int,
    txn_id: int,
    force: bool = Query(
        False,
        description="强制删除：先解绑场景/方案引用再删交易（A3/A4 后生效）",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除交易（owner+）：遵循「预检 + force」模式。

    A3/A4 场景或测试方案引用交易后，严格模式存在引用时拒绝（3053），
    需先解绑或 force；当前阶段交易无引用方，严格模式直接删除。
    """
    await ensure_project_access(db, project_id, user, "owner")
    txn = await _get_scoped_transaction(db, project_id, txn_id)

    # A3/A4 落地后在此统计引用场景/方案数，
    # 严格模式引用数 > 0 抛 3053，force 模式先解除引用再删除
    referencing_scenarios = 0
    referencing_plans = 0
    if not force and (referencing_scenarios or referencing_plans):
        raise BusinessError(
            f"交易被 {referencing_scenarios} 个场景、{referencing_plans} 个测试方案引用，"
            "无法删除；请先解绑引用或携带 force=true 强制删除",
            code=3053,
        )

    await db.delete(txn)
    await db.commit()
    return ok(
        {
            "id": txn_id,
            "deleted": True,
            "force": force,
            "removed_scenarios": referencing_scenarios if force else 0,
            "removed_test_plans": referencing_plans if force else 0,
        }
    )
