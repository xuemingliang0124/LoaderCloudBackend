"""场景-脚本关联表：一个场景可组合多个脚本，每个脚本单独指定压力机。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IntPkMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.scenario import Scenario
    from app.models.scenario_script_tg import ScenarioScriptTG
    from app.models.script import Script


class ScenarioScript(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "scenario_script"
    __table_args__ = (
        UniqueConstraint("scenario_id", "script_id", name="uq_scenario_script"),
    )

    scenario_id: Mapped[int] = mapped_column(ForeignKey("test_scenario.id"))
    script_id: Mapped[int] = mapped_column(ForeignKey("jmeter_script.id"))
    # 脚本在场景中的执行顺序（从 0 开始）
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # 本脚本的压力机选择：按 Agent 标签过滤，如 ["机房A"]
    agent_tags: Mapped[list | None] = mapped_column(JSON, default=list)
    # 本脚本需要的压力机数量
    agent_count: Mapped[int] = mapped_column(Integer, default=1)

    scenario: Mapped[Scenario] = relationship(back_populates="scripts")
    script: Mapped[Script] = relationship()
    thread_groups: Mapped[list[ScenarioScriptTG]] = relationship(
        back_populates="scenario_script", cascade="all, delete-orphan"
    )
