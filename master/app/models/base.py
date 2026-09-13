"""ORM 基类与公共 Mixin：所有表统一自增主键 + 时间戳。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IntPkMixin:
    # sqlite（测试内存库）要求 INTEGER PRIMARY KEY 才有 rowid 自增；
    # with_variant 仅作用于 sqlite 方言，MySQL 仍渲染 BIGINT
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
