"""Agent 结果分片表：结果汇聚的持久化落点（替代内存态 _pending_results）。

每个 Agent 执行结束上报 result 后落一行；收齐 run 下全部 Agent 后，
orchestrator 合并写 ES pt-summary 并置执行终态。Master 重启后据此恢复汇聚现场。
"""

from sqlalchemy import JSON, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin


class RunAgentResult(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "run_agent_result"
    __table_args__ = (
        UniqueConstraint("run_no", "agent_id", name="uq_run_agent_result"),
    )

    run_no: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    # Agent 上报的 summary（失败时为 {"failed": true, "message": ...}）
    summary: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 产物清单 [{"type": "jtl", "key": "runs/..."}]
    artifacts: Mapped[list | None] = mapped_column(JSON, default=list)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
