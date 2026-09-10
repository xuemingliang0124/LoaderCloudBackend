"""agent 配置：环境变量 + pydantic-settings。"""

import platform
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Master WebSocket 地址（含端点路径）
    master_ws_url: str = "ws://127.0.0.1:8000/ws/agent"
    # 手动指定 Agent 标识；留空则启动时按宿主机 IP 向 Master 注册获取固定 ID
    agent_id: str = ""
    # 逗号分隔分组标签，如 "机房A,高配"
    tags: str = ""
    # JMeter 可执行文件路径
    jmeter_bin: str = "jmeter"
    work_dir: str = "./agent_workspace"
    # 运行期插件目录：Master 下发的第三方插件 jar 落这里并通过
    # -Jsearch_paths 注入 JMeter 类路径（免改镜像）；空则默认 work_dir/plugins
    plugin_dir: str = ""
    heartbeat_interval: int = 10
    metrics_interval: int = 5
    reconnect_delay_max: int = 30

    @property
    def plugin_dir_path(self) -> str:
        if self.plugin_dir:
            return str(Path(self.plugin_dir).resolve())
        return str((Path(self.work_dir).resolve() / "plugins"))

    @property
    def resolved_agent_id(self) -> str:
        return self.agent_id or platform.node()

    @property
    def master_http_base(self) -> str:
        """从 WS 地址推导 HTTP 基址（ws→http / wss→https），用于注册等 REST 调用。"""
        parsed = urlparse(self.master_ws_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{scheme}://{parsed.netloc}"

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
