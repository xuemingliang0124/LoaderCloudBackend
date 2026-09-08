"""场景执行记录表：指标不在此存储（走 ES）。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SAEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin
from app.models.enums import RunStatus, RunTrigger


class ScenarioRun(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "scenario_run"

    run_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("test_scenario.id"))
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, length=16), default=RunStatus.PENDING
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        SAEnum(RunTrigger, length=16), default=RunTrigger.MANUAL
    )
    # 下发时的 Agent 列表快照
    agent_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    error_message: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
