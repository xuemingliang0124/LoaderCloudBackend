"""测试场景表：脚本 + 参数覆盖 + 压力机选择策略。"""

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class Scenario(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "test_scenario"

    name: Mapped[str] = mapped_column(String(128), index=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("jmeter_script.id"))
    # 参数覆盖 {"threads": "200", "ramp_up": "30"}，执行时拼 -J 参数
    param_overrides: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 按 Agent 标签选压力机，如 ["机房A"]
    agent_tags: Mapped[list | None] = mapped_column(JSON, default=list)
    agent_count: Mapped[int] = mapped_column(Integer, default=1)
    # 持续时长（秒），作为 -Jduration 覆盖
    duration: Mapped[int] = mapped_column(Integer, default=300)
    description: Mapped[str] = mapped_column(String(512), default="")
