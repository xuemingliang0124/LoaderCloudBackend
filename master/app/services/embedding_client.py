"""Embedding 调用层（D4，roadmap）：异步、批量、可重试。

- OpenAI 兼容 /embeddings 协议（智谱 embedding-3 / 阿里 text-embedding-v3 均兼容），
  基地址解析见 settings.embedding_base_url_resolved
- 必须用 httpx.AsyncClient（ptp-dev 3.1 强约束），禁止同步阻塞
- 批量化：单请求最多 embedding_batch_size（默认 64）段
- 失败指数退避重试 embedding_max_retries（默认 3）次，最终失败抛 EmbeddingError，
  由 D3 解析管道标记资产 FAILED
- 点位 ID 约定：point_id_for 生成确定性 int64（同资产重解析幂等覆盖旧向量）
"""

import asyncio
import zlib

import httpx
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


class EmbeddingClient:
    """OpenAI 兼容 embeddings 客户端（每实例独立配置，测试可注入覆盖）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.embedding_base_url_resolved).rstrip("/")
        self._api_key = api_key or settings.embedding_api_key
        self._model = model or settings.embedding_model
        self._batch_size = settings.embedding_batch_size
        self._timeout = settings.embedding_timeout
        self._max_retries = settings.embedding_max_retries
        if not self._base_url:
            raise EmbeddingError(
                "Embedding 未配置：请设置 EMBEDDING_PROVIDER（zhipu|dashscope）"
                "或 EMBEDDING_BASE_URL"
            )
        if not self._api_key or not self._model:
            raise EmbeddingError(
                "Embedding 未配置：请设置 EMBEDDING_API_KEY/EMBEDDING_MODEL"
            )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：按 batch_size 分批，保持输入顺序返回。"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """单批调用：指数退避重试，成功后按 index 排序保证与输入顺序一致。"""
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/embeddings",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={"model": self._model, "input": batch},
                    )
                resp.raise_for_status()
                data = resp.json()["data"]
                data.sort(key=lambda item: item.get("index", 0))
                if len(data) != len(batch):
                    raise EmbeddingError(
                        f"Embedding 返回段数不符：期望 {len(batch)}，实际 {len(data)}"
                    )
                return [item["embedding"] for item in data]
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                wait_seconds = 2 ** (attempt - 1)
                logger.warning(
                    f"Embedding 请求失败（第 {attempt}/{self._max_retries} 次），"
                    f"{wait_seconds}s 后重试: {exc}"
                )
                await asyncio.sleep(wait_seconds)
        raise EmbeddingError(
            f"Embedding 调用失败（已重试 {self._max_retries} 次）: {last_error}"
        ) from last_error
