"""用户表。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class User(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "sys_user"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    # 全局角色：admin（管理员）/ user（普通用户）；历史 viewer 同为非管理员
    role: Mapped[str] = mapped_column(String(32), default="user")
