"""项目成员管理：项目级授权（P3）。

接口以项目为作用域，统一挂在 /projects/{project_id}/members：
- 成员列表要求 viewer+；授权 / 改角色 / 移除要求 owner+（admin 全局直通）
- 目标用户不存在 3035；重复授权 3032；成员不存在 3033
- 受保护操作 3034：项目创建者不可移除/降级；项目内最后一个 owner 不可移除/降级
角色 DB 存英文小写（owner/editor/viewer），API 收/出中文（ProjectRole 枚举）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_project_access, get_current_user
from app.db.session import get_db
from app.models.enums import ProjectRole
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas import MemberGrantIn, MemberOut, MemberRoleUpdateIn
from app.schemas.common import like_pattern, ok
from app.services.exceptions import BusinessError

router = APIRouter()


def _role_to_cn(role: str) -> str:
    """DB 英文角色 → API 中文角色名。"""
    return ProjectRole[role.upper()].value


def _build_member_out(m: ProjectMember) -> dict:
    return MemberOut(
        id=m.id,
        project_id=m.project_id,
        username=m.username,
        role=_role_to_cn(m.role),
        granted_by=m.granted_by,
        created_at=m.created_at,
        updated_at=m.updated_at,
    ).model_dump(mode="json")


async def _get_member(
    db: AsyncSession, project_id: int, username: str
) -> ProjectMember:
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.username == username,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise BusinessError(f"成员不存在: {username}", code=3033)
    return member


async def _user_exists(db: AsyncSession, username: str) -> bool:
    return (
        await db.execute(select(User.id).where(User.username == username))
    ).scalar_one_or_none() is not None


def _assert_not_creator(project: Project, username: str, action: str) -> None:
    """项目创建者受保护：不可移除/降级。"""
    if project.created_by and username == project.created_by:
        raise BusinessError(f"项目创建者受保护，不可{action}", code=3034)


async def _assert_not_last_owner(db: AsyncSession, project_id: int) -> None:
    """项目内至少保留一个 owner。"""
    owner_count = (
        await db.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == "owner",
            )
        )
    ) or 0
    if int(owner_count) <= 1:
        raise BusinessError("项目至少需要保留一个项目管理员", code=3034)


@router.post("/projects/{project_id}/members")
async def grant_member(
    payload: MemberGrantIn,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """授权用户加入项目（owner+）：目标用户须存在（3035），不可重复授权（3032）。"""
    await ensure_project_access(db, project_id, user, "owner")
    if not await _user_exists(db, payload.username):
        raise BusinessError(f"目标用户不存在: {payload.username}", code=3035)
    existing = (
        await db.execute(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.username == payload.username,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BusinessError(f"该用户已是项目成员: {payload.username}", code=3032)

    member = ProjectMember(
        project_id=project_id,
        username=payload.username,
        role=payload.role.name.lower(),
        granted_by=user.username,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return ok(_build_member_out(member))


@router.get("/projects/{project_id}/members")
async def list_members(
    project_id: int,
    username: str | None = Query(default=None, description="按用户名模糊查询"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """项目内成员分页列表（viewer+）：支持用户名模糊查询，响应含 total。"""
    await ensure_project_access(db, project_id, user, "viewer")
    filters = [ProjectMember.project_id == project_id]
    if username:
        filters.append(
            ProjectMember.username.like(like_pattern(username.strip()), escape="\\")
        )

    total = await db.scalar(
        select(func.count()).select_from(ProjectMember).where(*filters)
    )
    rows = (
        (
            await db.execute(
                select(ProjectMember)
                .where(*filters)
                .order_by(ProjectMember.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [_build_member_out(m) for m in rows]
    return ok({"total": int(total or 0), "items": items})


@router.put("/projects/{project_id}/members/{username}")
async def update_member_role(
    username: str,
    payload: MemberRoleUpdateIn,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """变更成员角色（owner+）：创建者不可降级、最后一个 owner 不可降级（3034）。"""
    await ensure_project_access(db, project_id, user, "owner")
    project = await db.get(Project, project_id)
    member = await _get_member(db, project_id, username)
    new_role = payload.role.name.lower()

    if new_role != "owner":
        _assert_not_creator(project, username, "降级")
        if member.role == "owner":
            await _assert_not_last_owner(db, project_id)

    member.role = new_role
    await db.commit()
    await db.refresh(member)
    return ok(_build_member_out(member))


@router.delete("/projects/{project_id}/members/{username}")
async def remove_member(
    username: str,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """移除成员（owner+）：创建者不可移除、最后一个 owner 不可移除（3034）。"""
    await ensure_project_access(db, project_id, user, "owner")
    project = await db.get(Project, project_id)
    member = await _get_member(db, project_id, username)

    _assert_not_creator(project, username, "移除")
    if member.role == "owner":
        await _assert_not_last_owner(db, project_id)

    await db.delete(member)
    await db.commit()
    return ok({"project_id": project_id, "username": username, "removed": True})
