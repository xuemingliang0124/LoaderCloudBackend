"""公共依赖：JWT 登录态 + 项目作用域/成员授权门禁（P2 权限收口唯一入口）。"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token_payload
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services.exceptions import BusinessError

_bearer = HTTPBearer(auto_error=False)

# 项目成员角色等级：数字越大权限越高（owner ⊃ editor ⊃ viewer）
_ROLE_ORDER = {"viewer": 1, "editor": 2, "owner": 3}


@dataclass
class CurrentUser:
    """JWT 解析出的轻量身份（不触库）：全局角色随 token 携带。

    项目级成员关系（project_member）不进 token，保证授权回收即时生效。
    """

    username: str
    role: str


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_token_payload(credentials.credentials)
    # 缺 sub/role 的 token（含改造前旧 token）一律视为无效，强制重新登录
    if payload is None or not payload.get("sub") or not payload.get("role"):
        raise HTTPException(status_code=401, detail="登录已过期")
    return CurrentUser(username=payload["sub"], role=payload["role"])


def ensure_global_admin(user: CurrentUser) -> None:
    """全局管理员门禁（用户管理等全局能力）：非 admin 拒绝（1010）。

    纯 token role 判定、不触库，供 handler 首行显式调用，
    保持 401 → 422（query/body 校验）→ 1010 的顺序约定。
    """
    if user.role != "admin":
        raise BusinessError("仅管理员可执行该操作", code=1010)


async def ensure_project_access(
    db: AsyncSession, project_id: int, user: CurrentUser, required: str = "viewer"
) -> None:
    """项目作用域 + 成员授权校验：P2 起所有项目嵌套路由的唯一门禁。

    供各 handler 在函数内首行显式调用（延续既有约定，不做 Depends 依赖注入：
    FastAPI 依赖解析先于 query/body 校验，Depends 化会破坏 401 → 422 的顺序）。

    校验顺序：3021 项目不存在 → 3030 非成员 → 3031 角色不足。
    admin 全局超管仅校验项目存在性；非管理员热路径先查成员角色（单查询），
    未命中再回查项目以区分 3021/3030，不向非成员泄露项目存在性。
    未知 role 值按 3031 fail-closed。
    """
    if user.role == "admin":
        exists = (
            await db.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
        if exists is None:
            raise BusinessError(f"项目不存在: {project_id}", code=3021)
        return

    role = (
        await db.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.username == user.username,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        exists = (
            await db.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
        if exists is None:
            raise BusinessError(f"项目不存在: {project_id}", code=3021)
        raise BusinessError("无该项目访问权限", code=3030)
    if _ROLE_ORDER.get(role, 0) < _ROLE_ORDER.get(required, 99):
        raise BusinessError("权限不足", code=3031)


async def ensure_run_visible(db: AsyncSession, run_no: str, user: CurrentUser) -> None:
    """执行记录可见性（/metrics/timeseries 与 /ws/runs/{run_no} 共用）。

    run_no → 场景 → 项目派生归属，要求项目内 viewer 及以上；
    记录不存在 2003（与 runs.py _get_scoped_run 口径一致），
    项目级校验复用 ensure_project_access（3021/3030/3031）。
    """
    project_id = (
        await db.execute(
            select(Scenario.project_id)
            .join(ScenarioRun, ScenarioRun.scenario_id == Scenario.id)
            .where(ScenarioRun.run_no == run_no)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise BusinessError("执行记录不存在", code=2003)
    await ensure_project_access(db, project_id, user, required="viewer")
