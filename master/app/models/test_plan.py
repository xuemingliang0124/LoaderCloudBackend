"""测试方案表：项目内场景编排单元（P1 结构化资产 A4）。

方案把多个场景按顺序/权重组织为一次完整的测试活动，并定义整体通过判据
（pass_criteria 自由 JSON，如 {"max_p95_ms": 500, "max_error_rate": 1.0}，
由报告/LLM 模块解释，本表不约束内部结构）。

挂载关系落在 test_plan_scenario 弱关联表：场景可被多个方案复用，
方案删除不级联删除场景（保持 Scenario 可独立执行）；
场景删除走删除预检（严格 3017 阻断，force 解绑关联行）。
(project_id, name) 项目内唯一：方案名称即业务标识，防误建重复方案。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IntPkMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.test_plan_scenario import TestPlanScenario


class TestPlan(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_plan"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_test_plan_name"),
    )

    # 所属项目：方案为项目内资产，所有方案接口均按项目作用域嵌套访问
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("test_project.id", name="fk_test_plan_project"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    # 整体通过判据（自由 JSON）：{"max_p95_ms": 500, "max_error_rate": 1.0, "min_tps": 100}
    # 结构由报告/LLM 模块解释，本表仅持久化
    pass_criteria: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 报告模板标识：L4 报告生成时选用，默认 default
    report_template: Mapped[str] = mapped_column(String(64), default="default")
    description: Mapped[str] = mapped_column(String(512), default="")

    # 挂载的场景关联行（含 seq/weight），创建/更新时一并落库；
    # ORM 级联 delete-orphan：删除方案仅清理关联行，不触碰场景本身
    plan_scenarios: Mapped[list[TestPlanScenario]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="TestPlanScenario.seq",
    )
