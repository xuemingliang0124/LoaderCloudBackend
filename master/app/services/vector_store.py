"""向量存储抽象层 + Qdrant 实现。

职责（D2 引入，roadmap D2/D3/D4/L3/L4 均依赖本模块）：
- pt-knowledge 语义检索（RAG 召回）
- ES 不再承担 dense_vector 职责，仅保留 pt-summary/pt-metrics

抽象层设计（降低未来切 Milvus 成本，业务层 0 改动）：
- `VectorStore` Protocol 定义 ensure_collection / upsert_chunks / search 三个方法
- `QdrantVectorStore` 实现具体 SDK 调用
- `get_vector_store()` 工厂方法按 settings.vector_provider 分发
- 业务层（asset_parser/embedding_client/assets.py）禁止 import Qdrant SDK，
  只依赖 Protocol

迁移友好约定（落地时必须遵守，见 docs/roadmap-todo.md D2 章节）：
- chunk 主键用 int64 自增（Qdrant/Milvus 均原生支持；勿用 UUID）
- filter 用 Python dict 表达，由各 Store 实现翻译为各自语法
  （Qdrant FieldCondition / Milvus expr 字符串）
- 配置驱动切换：VECTOR_PROVIDER=qdrant|milvus
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from loguru import logger

from app.core.config import get_settings


@dataclass
class ChunkDoc:
    """待写入向量库的文本切片（业务层构造，Store 实现消费）。

    point_id 由业务层分配 int64（建议时间戳 + 顺序号，或 DB 自增）；
    各 Store 实现按自身需求转换（Qdrant 直接用 int64，Milvus 同）。
    """

    point_id: int
    asset_id: int
    project_id: int
    chunk_index: int
    text_chunk: str
    embedding: list[float]
    asset_type: str = ""
    source_type: str = ""
    source_ref: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class ChunkHit:
    """向量检索命中的切片（Store 实现返回，业务层消费）。"""

    point_id: int
    score: float
    payload: dict


class VectorStore(Protocol):
    """向量库协议。新增 Milvus 等实现只需实现本协议，业务层 0 改动。"""

    async def ensure_collection(self) -> None:
        """幂等创建 collection（启动期调用，参照 es_client.ensure_indices 范式）。"""
        ...

    async def upsert_chunks(self, points: list[ChunkDoc]) -> None:
        """批量写入/更新向量与 payload。失败抛异常，由调用方标记 asset FAILED。"""
        ...

    async def search(
        self,
        project_id: int,
        query_vec: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[ChunkHit]:
        """kNN 召回。project_id 强制过滤（多租户隔离），filters 为额外 payload 过滤。

        filters 用 dict 表达（如 {"asset_type": "sla_doc"}），由 Store 实现翻译为
        各自语法（Qdrant FieldCondition / Milvus expr 字符串）。
        """
        ...


class QdrantVectorStore:
    """Qdrant 实现：Rust 单容器 + async SDK 原生，契合 ptp-dev 3.1 异步约束。

    SDK：qdrant-client.AsyncQdrantClient
    - collection 用 payload 模型（自由 dict，不预定义 schema）
    - 向量索引 HNSW + COSINE
    - filter 用 FieldCondition（翻译自业务层 dict）
    """

    def __init__(self) -> None:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.http import models as qmodels

        self._qmodels = qmodels
        settings = get_settings()
        self._client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=10,
        )
        self._collection = settings.qdrant_collection
        self._dims = settings.embedding_dims

    async def ensure_collection(self) -> None:
        """幂等创建 collection（启动期调用）。

        参照 es_client.ensure_indices：已存在则跳过，不存在则用
        VectorParams(size, COSINE) 创建。不重建以保护存量数据。
        """
        client = self._client
        qmodels = self._qmodels
        collections = await client.get_collections()
        names = {c.name for c in (collections.collections or [])}
        if self._collection in names:
            logger.info(f"Qdrant collection {self._collection} 已存在，跳过创建")
            return
        await client.create_collection(
            collection_name=self._collection,
            vectors_config=qmodels.VectorParams(
                size=self._dims, distance=qmodels.Distance.COSINE
            ),
        )
        logger.info(f"已创建 Qdrant collection {self._collection} (dims={self._dims})")

    async def upsert_chunks(self, points: list[ChunkDoc]) -> None:
        """批量 upsert。空列表直接返回（避免 Qdrant 报错）。

        point_id 用 int64（业务层已分配）；payload 写所有元数据字段，
        vector 字段固定名为 embedding。
        """
        if not points:
            return
        client = self._client
        qmodels = self._qmodels
        records = [
            qmodels.PointStruct(
                id=p.point_id,
                vector={"embedding": p.embedding},
                payload={
                    "asset_id": p.asset_id,
                    "project_id": p.project_id,
                    "asset_type": p.asset_type,
                    "chunk_index": p.chunk_index,
                    "text_chunk": p.text_chunk,
                    "source_type": p.source_type,
                    "source_ref": p.source_ref,
                    "created_at": p.created_at.isoformat(),
                },
            )
            for p in points
        ]
        await client.upsert(collection_name=self._collection, points=records)
        logger.debug(f"Qdrant upsert: collection={self._collection} count={len(records)}")

    async def search(
        self,
        project_id: int,
        query_vec: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[ChunkHit]:
        """kNN 召回。project_id 必过滤，filters 为额外 payload 条件。

        filter 翻译：dict 形如 {"asset_type": "sla_doc"} →
        [FieldCondition(key="project_id", match=MatchValue(project_id)),
         FieldCondition(key="asset_type", match=MatchValue("sla_doc"))]
        """
        client = self._client
        qmodels = self._qmodels
        conditions = [
            qmodels.FieldCondition(
                key="project_id", match=qmodels.MatchValue(value=project_id)
            )
        ]
        for k, v in (filters or {}).items():
            conditions.append(
                qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v))
            )
        resp = await client.search(
            collection_name=self._collection,
            query_vector=query_vec,
            query_filter=qmodels.Filter(must=conditions),
            limit=top_k,
            with_payload=True,
        )
        return [
            ChunkHit(
                point_id=hit.id,
                score=hit.score or 0.0,
                payload=dict(hit.payload or {}),
            )
            for hit in resp
        ]


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """工厂方法：按 settings.vector_provider 返回单例。

    新增 Milvus 实现：在此加分支即可，业务层 0 改动。
    """
    global _store
    if _store is not None:
        return _store
    provider = get_settings().vector_provider.lower()
    if provider == "qdrant":
        _store = QdrantVectorStore()
        return _store
    raise NotImplementedError(
        f"VECTOR_PROVIDER={provider} 暂未实现；当前仅支持 qdrant"
    )


def reset_vector_store() -> None:
    """测试辅助：清空单例（测试夹具用）。"""
    global _store
    _store = None
