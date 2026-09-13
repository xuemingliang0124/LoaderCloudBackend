"""测试项目表：项目是脚本/场景等测试资产的顶层分组。

当前只承载项目基础信息（名称唯一），脚本与场景的项目归属后续按需挂接。
"""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Project(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_project"
    __table_args__ = (UniqueConstraint("name", name="uq_test_project_name"),)

    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
