"""场景内脚本线程组设置：保存每个线程组在场景下的加压参数。

执行时由 jmx_assembler 直接改写执行用 JMX 的 XML（原始脚本不动）：
enabled 写入线程组节点启用状态，num_threads / ramp_time / scheduler /
duration 写入线程组属性，tps ×60 换算为 TPM 写入线程组内常量吞吐量定时器。
循环次数不再落库：非基准场景统一无限循环（场景时长收口），
单交易基准固定 100 次（jmx_assembler 内常量）。
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
    # 场景级启用开关：组装时写入线程组节点 enabled 属性；false 时整组不执行
    # （初始值取脚本扫描结果，可在场景设置中切换）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    num_threads: Mapped[int] = mapped_column(Integer, default=1)
    ramp_time: Mapped[int] = mapped_column(Integer, default=0)
    # 集群目标 TPS（每秒样本数），0 表示不限速；多机执行时按 CPU 权重均摊
    # （允许小数份额），执行期份额 ×60 写入常量吞吐量定时器
    tps: Mapped[int] = mapped_column(Integer, default=0)
    scheduler: Mapped[bool] = mapped_column(Boolean, default=True)
    # 落库固定 scheduler=True，duration 取场景级运行时间；
    # 单交易基准执行期固定参数另行覆盖（scheduler=False、duration=0）
    duration: Mapped[int] = mapped_column(Integer, default=0)

    scenario_script: Mapped[ScenarioScript] = relationship(
        back_populates="thread_groups"
    )
