"""Agent 状态机：单事件循环访问，无需加锁。"""

from enum import Enum


class AgentPhase(str, Enum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    RUNNING = "running"
    UPLOADING = "uploading"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


class AgentState:
    """当前状态持有：phase + 正在执行的 run_id。"""

    def __init__(self) -> None:
        self.phase: AgentPhase = AgentPhase.IDLE
        self.current_run_id: str | None = None

    def set(self, phase: AgentPhase, run_id: str | None = None) -> None:
        self.phase = phase
        if run_id is not None:
            self.current_run_id = run_id
        if phase == AgentPhase.IDLE:
            self.current_run_id = None

    def to_payload(self) -> dict:
        return {"status": self.phase.value, "current_run_id": self.current_run_id}
