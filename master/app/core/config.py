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

    # 向量库（D2 引入：RAG 语义检索；ES 不再承担 dense_vector 职责）
    # vector_provider：qdrant（当前实现）/ milvus（待实现）；切换走工厂方法
    vector_provider: str = "qdrant"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "pt_knowledge"
    # embedding 向量维度；须与 D4 Embedding 模型输出一致
    # 智谱 embedding-3 / 阿里 text-embedding-v3 均为 1024
    embedding_dims: int = 1024

    # Embedding 调用层（D4 引入，D3 解析管道消费）：OpenAI 兼容 /embeddings 协议
    # embedding_provider：zhipu | dashscope；空且未配 base_url 视为未启用
    # 未启用时解析管道仍做结构化抽取，仅跳过向量入库（parse_meta.indexed=false）
    embedding_provider: str = ""
    # OpenAI 兼容 /embeddings 基地址；显式配置优先于 provider 默认值
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    # 单请求最多段数 / 超时秒数 / 失败重试次数（指数退避后标记资产 FAILED）
    embedding_batch_size: int = 64
    embedding_timeout: int = 30
    embedding_max_retries: int = 3

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
    def embedding_base_url_resolved(self) -> str:
        """Embedding 基地址解析：显式配置 > provider 默认值；均无返回空串。"""
        if self.embedding_base_url:
            return self.embedding_base_url.rstrip("/")
        provider = self.embedding_provider.strip().lower()
        defaults = {
            "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
        return defaults.get(provider, "")

    @property
    def metrics_index_pattern(self) -> str:
        return f"{self.es_index_prefix}-metrics-*"

    @property
    def summary_index(self) -> str:
        return f"{self.es_index_prefix}-summary"


@lru_cache
def get_settings() -> Settings:
    return Settings()
