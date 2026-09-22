"""Embedding 调用层（D4 + P3 Stage 1 重构）：LangChain Embeddings 工厂 + 委托。

重构要点（SRS 1.5.3 + NFR-01）：
- 新增 `get_embeddings()` 工厂：返回 LangChain `Embeddings` 抽象
  - 已配置 EMBEDDING_API_KEY + EMBEDDING_MODEL → `OpenAIEmbeddings`（OpenAI 兼容 /embeddings）
  - 未配置 → `FakeEmbeddings(size=embedding_dims)` 降级（SRS 1.5.4：入库 indexed=true, degraded=true）
- `EmbeddingClient` 保留为兼容层：内部委托给 `get_embeddings()`，保留批量切片
  （LangChain OpenAIEmbeddings 自带重试，但批量切片逻辑保留以兼容存量调用方）
- `point_id_for` 确定性 int64 算法不变（与 QdrantVectorStore / langchain_qdrant 共享）

业务层（langchain_pipeline / asset_parser）应优先直接调 `get_embeddings()`；
`EmbeddingClient` 仅供 D3 旧测试兼容与未迁移调用方使用。
"""

import asyncio
import zlib

from loguru import logger

from app.core.config import get_settings


class EmbeddingError(RuntimeError):
    """Embedding 调用最终失败（重试耗尽 / 配置缺失）。"""


def point_id_for(asset_id: int, chunk_index: int) -> int:
    """确定性 int64 向量点 ID：asset_id + chunk_index 的 CRC32。

    同资产重解析时同索引切片幂等覆盖旧向量（Qdrant/Milvus 均原生 int64 主键）；
    注意：新解析切片数少于旧切片时，多余旧向量会残留（VectorStore 协议
    暂无按 filter 删除能力，后续扩展 delete_by_filter 时一并解决）。
    """
    return zlib.crc32(f"{asset_id}:{chunk_index}".encode())


# ---------- LangChain Embeddings 工厂（FR-04 + NFR-01 降级） ----------

_embeddings: object | None = None


def get_embeddings():
    """工厂：返回 LangChain `Embeddings` 抽象（单例）。

    - 已配置 EMBEDDING_API_KEY + EMBEDDING_MODEL → `langchain_openai.OpenAIEmbeddings`
      （OpenAI 兼容 /embeddings 协议；智谱 embedding-3 / 阿里 text-embedding-v3 均兼容）
    - 未配置 → `langchain_core.FakeEmbeddings(size=embedding_dims)` 降级
      （SRS 1.5.4：FakeEmbeddings 入库，indexed=true, degraded=true，链路不中断）

    业务层（langchain_pipeline.index_chunks）直接调本工厂；
    返回的 Embeddings 实现 `aembed_documents` / `aembed_query` 协议。
    """
    global _embeddings
    if _embeddings is not None:
        return _embeddings

    settings = get_settings()
    if settings.embedding_api_key and settings.embedding_model:
        from langchain_openai import OpenAIEmbeddings

        # OpenAIEmbeddings 字段：model / api_key(→openai_api_key) /
        # base_url(→openai_api_base) / request_timeout / dimensions
        # （embedding_ctx_length 是 token 上限，不是向量维度；max_retries 走
        #   retry_min_seconds/retry_max_seconds 默认值，不显式传）
        # check_embedding_ctx_length=False：默认 LangChain 会先用 tiktoken/transformers
        # 把文本分词成 token IDs 列表再传 input=[int,...]，OpenAI 原生端点支持，
        # 但 DashScope/智谱 OpenAI 兼容端点只接受 str | list[str]，收到 int 列表
        # 会 400 "contents is neither str nor list of str"；且国产模型 tokenizer 与
        # tiktoken 词表不匹配，分词本就无意义。禁用后走文本直传路径，对齐原生 SDK
        _embeddings = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url_resolved or None,
            request_timeout=settings.embedding_timeout,
            check_embedding_ctx_length=False,
        )
        logger.info(
            f"Embedding 已配置：model={settings.embedding_model}, "
            f"base_url={settings.embedding_base_url_resolved}"
        )
        return _embeddings

    # NFR-01 降级：FakeEmbeddings 入库（不跳过）
    from langchain_core.embeddings import FakeEmbeddings

    _embeddings = FakeEmbeddings(size=settings.embedding_dims)
    logger.warning(
        "Embedding 未配置，降级使用 FakeEmbeddings 入库（indexed=true, degraded=true）"
    )
    return _embeddings


def reset_embeddings() -> None:
    """测试辅助：清空 Embeddings 单例。"""
    global _embeddings
    _embeddings = None


# ---------- 兼容层：EmbeddingClient（委托 LangChain Embeddings） ----------


class EmbeddingClient:
    """OpenAI 兼容 embeddings 客户端（兼容层，委托给 `get_embeddings()`）。

    保留批量切片逻辑（按 embedding_batch_size 分批），每批调 LangChain
    `Embeddings.aembed_documents`；LangChain OpenAIEmbeddings 自带指数退避重试。
    新代码应直接用 `get_embeddings()` 工厂。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        # 兼容旧签名：构造时校验配置（未配置抛 EmbeddingError，与旧测试期望一致）
        settings = get_settings()
        resolved_base = (base_url or settings.embedding_base_url_resolved).rstrip("/")
        resolved_key = api_key or settings.embedding_api_key
        resolved_model = model or settings.embedding_model
        if not resolved_base:
            raise EmbeddingError(
                "Embedding 未配置：请设置 EMBEDDING_PROVIDER（zhipu|dashscope）"
                "或 EMBEDDING_BASE_URL"
            )
        if not resolved_key or not resolved_model:
            raise EmbeddingError(
                "Embedding 未配置：请设置 EMBEDDING_API_KEY/EMBEDDING_MODEL"
            )
        self._embeddings = get_embeddings()
        self._batch_size = settings.embedding_batch_size

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：按 batch_size 分批委托 LangChain Embeddings，保持输入顺序返回。"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批调用 LangChain Embeddings.aembed_documents（自带重试）。"""
        try:
            return await self._embeddings.aembed_documents(batch)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Embedding 调用失败: {exc}")
            # 兼容旧测试期望：失败重试耗尽后抛 EmbeddingError
            await asyncio.sleep(0)
            raise EmbeddingError(f"Embedding 调用失败: {exc}") from exc
