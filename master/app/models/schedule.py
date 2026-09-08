"""定时场景表：cron 触发，APScheduler 持久化 job 与此联动。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class ScheduleJob(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "schedule_job"

    name: Mapped[str] = mapped_column(String(128))
    scenario_id: Mapped[int] = mapped_column(ForeignKey("test_scenario.id"))
    # 标准 5 段 crontab 表达式：分 时 日 月 周
    cron: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_run_no: Mapped[str | None] = mapped_column(String(64), default=None)
