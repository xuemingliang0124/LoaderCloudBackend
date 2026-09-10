"""全局配置：环境变量 + pydantic-settings，禁止硬编码地址/密钥。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 应用
    app_name: str = "JMeter PT Platform Master"
    debug: bool = False
    secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    # MySQL
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "ptp"
    mysql_password: str = "ptp123456"
    mysql_db: str = "ptp"

    # Elasticsearch
    es_url: str = "http://127.0.0.1:9200"
    es_index_prefix: str = "pt"
    es_metrics_retention_days: int = 90

    # MinIO
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ptp"
    minio_secure: bool = False
    # 生成 presigned URL 使用的对外地址（必须 Agent 侧可达，如 "192.168.1.10:9000"）；
    # 空则回退 minio_endpoint，仅适用于 Agent 与 MinIO 同 Docker 网络的部署
    minio_public_endpoint: str = ""

    # Agent 通信
    agent_heartbeat_interval: int = 10
    agent_offline_threshold: int = 3
    # 停止等待 Agent 回报终态的超时秒数（看门狗兜底强制置 STOPPED）
    stop_wait_timeout: int = 90

    # 调度
    scheduler_enabled: bool = True
    timezone: str = "Asia/Shanghai"

    @property
    def mysql_dsn(self) -> str:
        """异步 DSN（aiomysql）。"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def mysql_dsn_sync(self) -> str:
        """同步 DSN（APScheduler SQLAlchemyJobStore 使用）。"""
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def metrics_index_pattern(self) -> str:
        return f"{self.es_index_prefix}-metrics-*"

    @property
    def summary_index(self) -> str:
        return f"{self.es_index_prefix}-summary"


@lru_cache
def get_settings() -> Settings:
    return Settings()
