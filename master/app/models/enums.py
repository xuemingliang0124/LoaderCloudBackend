"""领域枚举：全部状态集中定义，禁止魔法字符串。"""

from enum import Enum


class AgentStatus(str, Enum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"


class AgentPhase(str, Enum):
    """Agent 单任务生命周期阶段（status 消息上报）。"""

    DOWNLOADING = "downloading"
    RUNNING = "running"
    UPLOADING = "uploading"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
