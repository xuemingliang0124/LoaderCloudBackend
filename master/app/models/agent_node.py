"""压力机节点表：Agent 注册中心的数据落点。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SAEnum, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin, TimestampMixin
from app.models.enums import AgentStatus


class AgentNode(Base, IntPkMixin, TimestampMixin):
    __tablename__ = "agent_node"

    agent_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 宿主机 IP：Agent 启动时按此查固定 agent_id，建索引
    ip: Mapped[str] = mapped_column(String(64), default="", index=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    # 分组标签，如 ["机房A", "高配"]
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    jmeter_version: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(AgentStatus, length=16), default=AgentStatus.OFFLINE
    )
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    mem_percent: Mapped[float] = mapped_column(Float, default=0.0)
    current_run_no: Mapped[str | None] = mapped_column(String(64), default=None)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, default=None)
