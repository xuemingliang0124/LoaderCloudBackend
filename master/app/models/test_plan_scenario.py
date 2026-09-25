"""测试方案-场景弱关联表（P1 结构化资产 A4）。

记录方案挂载的场景集合及执行顺序/权重：
- scenario_id FK 默认 RESTRICT（不级联删除场景，保持 Scenario 可独立执行）；
  场景删除走预检（严格 3017 阻断），force 模式应用层解绑关联行
- plan_id FK 默认 RESTRICT；方案删除由 ORM 级联（TestPlan.plan_scenarios
  delete-orphan）或项目 force 删除在应用层 bulk delete 关联行
- (plan_id, scenario_id) 唯一：同一场景在同一方案内不可重复挂载
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IntPkMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.scenario import Scenario
    from app.models.test_plan import TestPlan


class TestPlanScenario(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_plan_scenario"
    __table_args__ = (
        UniqueConstraint("plan_id", "scenario_id", name="uq_test_plan_scenario"),
    )

    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_plan.id", name="fk_test_plan_scenario_plan"),
        index=True,
    )
    scenario_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_scenario.id", name="fk_test_plan_scenario_scenario"),
        index=True,
    )
    # 执行顺序：一键批量执行时按 seq 升序串行触发
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # 权重：预留给报告聚合/流量配比语义，当前仅持久化
    weight: Mapped[int] = mapped_column(Integer, default=1)

    plan: Mapped[TestPlan] = relationship(back_populates="plan_scenarios")
    # 供详情/列表装配 scenario_name；访问处必须 selectinload（async 禁懒加载）
    scenario: Mapped[Scenario] = relationship()
