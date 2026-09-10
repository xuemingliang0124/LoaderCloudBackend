"""场景内脚本线程组设置：保存每个线程组在场景下的加压参数。

执行时转为 -J 参数注入（脚本内线程组属性需用 ${__P(key, default)} 引用），
约定 key：threads_<线程组名>、ramp_up_<线程组名>、loops_<线程组名>、
duration_<线程组名>。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IntPkMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.scenario_script import ScenarioScript


class ScenarioScriptTG(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "scenario_script_tg"
    __table_args__ = (
        UniqueConstraint(
            "scenario_script_id", "thread_group_name", name="uq_scenario_script_tg"
        ),
    )

    scenario_script_id: Mapped[int] = mapped_column(ForeignKey("scenario_script.id"))
    # 线程组名称（对应 JMX 中 ThreadGroup.testname）
    thread_group_name: Mapped[str] = mapped_column(String(128))
    # 线程组类型：ThreadGroup / SetUpThreadGroup / TearDownThreadGroup
    testclass: Mapped[str] = mapped_column(String(64), default="ThreadGroup")
    num_threads: Mapped[int] = mapped_column(Integer, default=1)
    ramp_time: Mapped[int] = mapped_column(Integer, default=0)
    loops: Mapped[int] = mapped_column(Integer, default=1)  # -1 表示无限循环
    scheduler: Mapped[bool] = mapped_column(Boolean, default=False)
    duration: Mapped[int] = mapped_column(Integer, default=0)  # scheduler=false 时为 0

    scenario_script: Mapped[ScenarioScript] = relationship(
        back_populates="thread_groups"
    )
