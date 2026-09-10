"""压力机节点表：Agent 注册中心的数据落点。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SAEnum, Float, Integer, String
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
    # 已安装的 JMeter 插件 jar 文件名（lib/ext + 运行期 plugin_dir 扫描结果）
    plugins: Mapped[list | None] = mapped_column(JSON, default=list)
    # 压力机规格：逻辑核数 / 内存总量 GB（线程按规格拆分用）
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    mem_total_gb: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(AgentStatus, length=16), default=AgentStatus.OFFLINE
    )
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    mem_percent: Mapped[float] = mapped_column(Float, default=0.0)
    current_run_no: Mapped[str | None] = mapped_column(String(64), default=None)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, default=None)
