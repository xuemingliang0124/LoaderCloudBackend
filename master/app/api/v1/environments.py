"""环境清单管理：项目内被测环境资产 CRUD + 删除预检。

全部环境接口以项目为作用域，统一使用 /projects/{project_id}/environments 嵌套路由：
- 项目门禁统一走 ensure_project_access：viewer+ 查询、editor+ 增改、owner+ 删除
- 项目内 env_code 唯一，重复返回 3040
- 操作具体环境时校验归属：不存在 3041，不属于该项目 3042
- 删除遵循「预检 + force」模式（与脚本/场景/项目口径一致）：
  A3 场景绑定环境后，被场景引用的环境严格模式拒绝（3043），
  force=true 先解绑场景引用再删；当前阶段暂无引用方，严格模式恒可删除。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.environment import Environment
from app.schemas import EnvironmentIn, EnvironmentOut, EnvironmentUpdateIn
from app.schemas.common import like_pattern, ok
from app.services.exceptions import BusinessError

router = APIRouter()


async def _get_scoped_environment(
    db: AsyncSession, project_id: int, env_id: int
) -> Environment:
    """按项目作用域取环境：不存在 3041，跨项目访问 3042。"""
    env = (
        await db.execute(select(Environment).where(Environment.id == env_id))
    ).scalar_one_or_none()
    if env is None:
        raise BusinessError("环境不存在", code=3041)
    if env.project_id != project_id:
        raise BusinessError("环境不属于指定项目", code=3042)
    return env


async def _check_env_code_available(
    db: AsyncSession, project_id: int, env_code: str, exclude_id: int | None = None
) -> None:
    """项目内 env_code 唯一校验：重复 3040（更新时排除自身）。"""
    stmt = select(Environment.id).where(
        Environment.project_id == project_id, Environment.env_code == env_code
    )
    if exclude_id is not None:
        stmt = stmt.where(Environment.id != exclude_id)
    dup = (await db.execute(stmt)).scalar_one_or_none()
    if dup is not None:
        raise BusinessError(f"项目内环境编码已存在: {env_code}", code=3040)


@router.post("/projects/{project_id}/environments")
async def create_environment(
    project_id: int,
    payload: EnvironmentIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """新建环境（editor+）：项目内 env_code 不可重复（3040）。"""
    await ensure_project_access(db, project_id, user, "editor")
    await _check_env_code_available(db, project_id, payload.env_code)

    env = Environment(
        project_id=project_id,
        name=payload.name,
        env_code=payload.env_code,
        base_url=payload.base_url,
        hosts=payload.hosts,
        db_connections=payload.db_connections,
        middleware_info=payload.middleware_info,
        variables=payload.variables,
        description=payload.description,
    )
    db.add(env)
    await db.commit()
    await db.refresh(env)
    return ok(EnvironmentOut.model_validate(env).model_dump(mode="json"))


@router.get("/projects/{project_id}/environments")
async def list_environments(
    project_id: int,
    name: str | None = Query(default=None, description="按环境名称模糊查询"),
    env_code: str | None = Query(default=None, description="按环境编码精确查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """环境分页列表（viewer+）：支持名称模糊/编码精确过滤，按 id 倒序，响应含 total。"""
    await ensure_project_access(db, project_id, user, "viewer")

    filters = [Environment.project_id == project_id]
    if name:
        filters.append(Environment.name.like(like_pattern(name.strip()), escape="\\"))
    if env_code:
        filters.append(Environment.env_code == env_code.strip())

    total = await db.scalar(
        select(func.count()).select_from(Environment).where(*filters)
    )
    rows = (
        (
            await db.execute(
                select(Environment)
                .where(*filters)
                .order_by(Environment.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [EnvironmentOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})


@router.get("/projects/{project_id}/environments/{env_id}")
async def get_environment(
    project_id: int,
    env_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """环境详情（viewer+）：不存在 3041，跨项目 3042。"""
    await ensure_project_access(db, project_id, user, "viewer")
    env = await _get_scoped_environment(db, project_id, env_id)
    return ok(EnvironmentOut.model_validate(env).model_dump(mode="json"))


@router.put("/projects/{project_id}/environments/{env_id}")
async def update_environment(
    project_id: int,
    env_id: int,
    payload: EnvironmentUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新环境（editor+）：env_code 变更后不可与项目内其他环境重复（3040）。"""
    await ensure_project_access(db, project_id, user, "editor")
    env = await _get_scoped_environment(db, project_id, env_id)

    if payload.env_code is not None and payload.env_code != env.env_code:
        await _check_env_code_available(
            db, project_id, payload.env_code, exclude_id=env_id
        )

    for field in (
        "name",
        "env_code",
        "base_url",
        "hosts",
        "db_connections",
        "middleware_info",
        "variables",
        "description",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(env, field, value)

    await db.commit()
    await db.refresh(env)
    return ok(EnvironmentOut.model_validate(env).model_dump(mode="json"))


@router.get("/projects/{project_id}/environments/{env_id}/delete-precheck")
async def precheck_environment_delete(
    project_id: int,
    env_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除前预检（viewer+）：返回引用该环境的场景数。

    A3 场景绑定环境（scenario.environment_id）落地后，此处统计引用数；
    当前阶段恒为 0。running_runs 等更细粒度预检随 A3 一并补充。
    """
    await ensure_project_access(db, project_id, user, "viewer")
    await _get_scoped_environment(db, project_id, env_id)
    return ok({"environment_id": env_id, "scenarios": 0})


@router.delete("/projects/{project_id}/environments/{env_id}")
async def delete_environment(
    project_id: int,
    env_id: int,
    force: bool = Query(
        False,
        description="强制删除：先解绑场景引用再删环境（A3 后生效）",
    ),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除环境（owner+）：遵循「预检 + force」模式。

    A3 场景绑定环境后，严格模式存在引用场景时拒绝（3043），需先解绑或 force；
    当前阶段环境无引用方，严格模式直接删除。
    """
    await ensure_project_access(db, project_id, user, "owner")
    env = await _get_scoped_environment(db, project_id, env_id)

    # A3 落地后在此统计引用场景数（Scenario.environment_id == env_id），
    # 严格模式引用数 > 0 抛 3043，force 模式先解除场景绑定再删除
    referencing_scenarios = 0
    if not force and referencing_scenarios:
        raise BusinessError(
            f"环境被 {referencing_scenarios} 个场景引用，无法删除；"
            "请先解绑场景或携带 force=true 强制删除",
            code=3043,
        )

    await db.delete(env)
    await db.commit()
    return ok(
        {
            "id": env_id,
            "deleted": True,
            "force": force,
            "removed_scenarios": referencing_scenarios if force else 0,
        }
    )
