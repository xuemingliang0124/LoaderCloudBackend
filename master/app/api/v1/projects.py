"""项目管理：项目是脚本/场景等测试资产的顶层分组（名称唯一）。

权限语义（P2）：任何登录用户可建项目，创建者在同一事务内自动成为 owner；
非 admin 用户的项目列表仅返回自己为成员的项目，admin 全量可见。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.db.session import get_db
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.schemas import ProjectIn, ProjectOut
from app.schemas.common import like_pattern, ok
from app.services.exceptions import BusinessError

router = APIRouter()


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
    return ok(ProjectOut.model_validate(project).model_dump(mode="json"))


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
    items = [ProjectOut.model_validate(r).model_dump(mode="json") for r in rows]
    return ok({"total": int(total or 0), "items": items})
