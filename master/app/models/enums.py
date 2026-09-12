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
    # 停止指令已下发，等待 Agent 回报终态；回报收齐（或看门狗超时）才置 STOPPED
    STOPPING = "stopping"
    FINISHED = "finished"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class ScenarioType(str, Enum):
    """压测场景类型，四选一。"""

    SINGLE_BASELINE = "单交易基准"
    SINGLE_LOAD = "单交易负载"
    MIXED = "混合场景"
    STABILITY = "稳定性"
