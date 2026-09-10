"""Agent 实际安装的插件版本（落库审计 + 灰度分发 + 卸载兜底）。

设计动机（替代 JSON 数组）：
- AgentNode.plugins 仅作 Agent 上报的快照冗余，权威数据在本表
- 关联表支持灰度（同插件可只装到 tag 子集）
- 卸载审计：删插件时知道哪些 Agent 实际清理过
- pending_remove：Agent 正在跑用该插件的任务时，标记延后清理
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPkMixin


class AgentPlugin(Base, IntPkMixin):
    __tablename__ = "agent_plugin"
    __table_args__ = (
        UniqueConstraint("agent_id", "plugin_id", name="uq_agent_plugin"),
    )

    agent_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_node.agent_id"), index=True
    )
    plugin_id: Mapped[int] = mapped_column(ForeignKey("jmeter_plugin.id"))
    # 冗余 sha256 便于 Master 心跳比对（避免每次 JOIN jmeter_plugin）
    installed_sha256: Mapped[str] = mapped_column(String(64), default="")
    installed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # installed / installing / failed / pending_remove
    # - pending_remove：Master 禁用插件时置此态，Agent 任务结束后清理
    status: Mapped[str] = mapped_column(String(16), default="installed")
