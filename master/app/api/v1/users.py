"""用户管理：全局用户 CRUD（仅全局管理员可访问）。

- 非 admin 一律拒绝（1010），admin 身份由 JWT role claim 判定
- 用户名唯一：重复创建 1011；目标用户不存在 1012
- 自保护：不可降级/删除自己（1014）
- 末位管理员保护：系统至少保留一个 admin（1015）
- 删除用户时在同一事务内级联清理 project_member（成员关系随账号失效）
角色 DB 存英文小写（admin/user），API 收/出中文（GlobalRole 枚举）；
历史 viewer 值按非管理员语义渲染。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_global_admin, get_current_user
from app.core.security import hash_password
from app.db.session import get_db
from app.models.enums import GlobalRole
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas import UserCreateIn, UserOut, UserUpdateIn
from app.schemas.common import like_pattern, ok
from app.services.exceptions import BusinessError

router = APIRouter()


def _role_to_cn(role: str) -> str:
    """DB 英文角色 → API 中文角色名；历史 viewer 等非 admin 值统一渲染为普通用户。"""
    if role == GlobalRole.ADMIN.name.lower():
        return GlobalRole.ADMIN.value
    return GlobalRole.USER.value


def _build_user_out(u: User) -> dict:
    return UserOut(
        id=u.id,
        username=u.username,
        role=_role_to_cn(u.role),
        created_at=u.created_at,
        updated_at=u.updated_at,
    ).model_dump(mode="json")


async def _get_user(db: AsyncSession, username: str) -> User:
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        raise BusinessError(f"用户不存在: {username}", code=1012)
    return user


async def _count_admins(db: AsyncSession) -> int:
    return int(
        (
            await db.scalar(
                select(func.count()).select_from(User).where(User.role == "admin")
            )
        )
        or 0
    )


@router.post("/users")
async def create_user(
    payload: UserCreateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """新建用户（admin）：用户名不可重复（1011）。"""
    ensure_global_admin(user)
    exists = (
        await db.execute(select(User.id).where(User.username == payload.username))
    ).scalar_one_or_none()
    if exists is not None:
        raise BusinessError(f"用户名已存在: {payload.username}", code=1011)

    new_user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role.name.lower(),
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return ok(_build_user_out(new_user))


@router.get("/users")
async def list_users(
    username: str | None = Query(default=None, description="按用户名模糊查询"),
    role: GlobalRole | None = Query(default=None, description="按全局角色过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """用户分页列表（admin）：支持用户名模糊、角色精确过滤，响应含 total。"""
    ensure_global_admin(user)
    filters = []
    if username:
        filters.append(User.username.like(like_pattern(username.strip()), escape="\\"))
    if role is not None:
        if role == GlobalRole.USER:
            # 普通用户过滤兼容历史 viewer 值：非 admin 即普通用户
            filters.append(User.role != "admin")
        else:
            filters.append(User.role == role.name.lower())

    total = await db.scalar(select(func.count()).select_from(User).where(*filters))
    rows = (
        (
            await db.execute(
                select(User)
                .where(*filters)
                .order_by(User.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [_build_user_out(u) for u in rows]
    return ok({"total": int(total or 0), "items": items})


@router.get("/users/{username}")
async def get_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """用户详情（admin）：用户不存在 1012。"""
    ensure_global_admin(user)
    target = await _get_user(db, username)
    return ok(_build_user_out(target))


@router.put("/users/{username}")
async def update_user(
    username: str,
    payload: UserUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """更新用户角色/密码（admin）：不可降级自己（1014），至少保留一个 admin（1015）。"""
    ensure_global_admin(user)
    target = await _get_user(db, username)

    if payload.role is not None:
        new_role = payload.role.name.lower()
        if new_role != "admin":
            if username == user.username:
                raise BusinessError("不可降级当前登录账号", code=1014)
            if target.role == "admin" and await _count_admins(db) <= 1:
                raise BusinessError("系统至少需要保留一个管理员", code=1015)
            target.role = new_role
        else:
            target.role = "admin"

    if payload.password is not None:
        target.password_hash = hash_password(payload.password)

    await db.commit()
    await db.refresh(target)
    return ok(_build_user_out(target))


@router.delete("/users/{username}")
async def delete_user(
    username: str,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """删除用户（admin）：不可删除自己（1014）、不可删除最后一个管理员（1015）。

    同事务级联清理 project_member；用户被删后其项目成员关系即时失效，
    若某项目因此失去唯一 owner，可由全局 admin 事后重新授权。
    """
    ensure_global_admin(user)
    if username == user.username:
        raise BusinessError("不可删除当前登录账号", code=1014)
    target = await _get_user(db, username)
    if target.role == "admin" and await _count_admins(db) <= 1:
        raise BusinessError("系统至少需要保留一个管理员", code=1015)

    await db.execute(delete(ProjectMember).where(ProjectMember.username == username))
    await db.delete(target)
    await db.commit()
    return ok({"username": username, "deleted": True})
