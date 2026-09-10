"""测试场景表：多脚本组合。

脚本关联（含各自的压力机选择策略）与线程组设置分别落在
scenario_script / scenario_script_tg 表，本表只存场景级配置
（名称、全局 JVM 参数覆盖）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IntPkMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.scenario_script import ScenarioScript


class Scenario(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_scenario"
    __table_args__ = (UniqueConstraint("name", name="uq_test_scenario_name"),)

    name: Mapped[str] = mapped_column(String(128), index=True)
    # 场景级 JVM 参数覆盖 {"host": "api.demo.com"}，执行时拼 -J 参数
    param_overrides: Mapped[dict | None] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(String(512), default="")

    # 关联脚本（含各脚本的压力机选择 + 线程组设置），创建时一并落库
    scripts: Mapped[list[ScenarioScript]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
