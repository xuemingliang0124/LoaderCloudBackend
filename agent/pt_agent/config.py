"""Agent 配置：环境变量 + pydantic-settings。"""

import platform
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Master WebSocket 地址（含端点路径）
    master_ws_url: str = "ws://127.0.0.1:8000/ws/agent"
    # 不配置时默认取主机名
    agent_id: str = ""
    # 逗号分隔分组标签，如 "机房A,高配"
    tags: str = ""
    # JMeter 可执行文件路径
    jmeter_bin: str = "jmeter"
    work_dir: str = "./agent_workspace"
    heartbeat_interval: int = 10
    metrics_interval: int = 5
    reconnect_delay_max: int = 30

    @property
    def resolved_agent_id(self) -> str:
        return self.agent_id or platform.node()

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
