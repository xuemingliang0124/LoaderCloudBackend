"""项目成员表：项目级授权基础（P1）。

身份以 username 字符串承载，与 test_project.created_by、JWT sub 口径一致
（sys_user.username 有唯一索引，可作逻辑主键；不做物理外键避免用户表耦合）。
role 存英文枚举名：owner（项目管理员）/ editor（可写）/ viewer（只读），
P2 由 ensure_project_access 统一消费。
"""

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class ProjectMember(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "project_member"
    __table_args__ = (
        UniqueConstraint("project_id", "username", name="uq_project_member"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", ondelete="CASCADE"),
        index=True,
    )
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    granted_by: Mapped[str] = mapped_column(String(64), default="")
