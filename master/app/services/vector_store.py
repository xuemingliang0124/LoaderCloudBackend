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
        命名向量 "embedding"（VectorParams(size, COSINE)）创建。不重建以保护存量数据。
        命名向量而非匿名向量：入库 upsert_chunks 与检索 PTPQdrant 均按
        vector_name="embedding" 读写，collection 必须以命名向量配置创建，
        否则 query_points(using="embedding") 会 400 "Vector params for
        embedding are not specified in config"。
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
            vectors_config={
                "embedding": qmodels.VectorParams(
                    size=self._dims, distance=qmodels.Distance.COSINE
                )
            },
        )
        logger.info(f"已创建 Qdrant collection {self._collection} (dims={self._dims}, vector=embedding)")

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


# ---------- LangChain VectorStore 适配（FR-04/05 双抽象共存） ----------

_lc_store: object | None = None


def get_langchain_vector_store():
    """工厂：返回 LangChain `langchain_qdrant.Qdrant` 子类实例（单例）。

    双抽象共存（SRS 1.5.3 策略 2）：
    - 现有 `VectorStore` Protocol + `QdrantVectorStore`：业务层入库（asset_parser）
    - LangChain `Qdrant` 子类：FR-05 检索链路用（as_retriever）

    对齐约束（与 QdrantVectorStore.upsert_chunks 的存储布局一致）：
    - 共享同一 collection（settings.qdrant_collection，pt_knowledge）与同一
      AsyncQdrantClient（复用 base._client）
    - content_payload_key="text_chunk"（入库文本字段名）
    - vector_name="embedding"（入库为命名向量；SDK 默认无名向量会查不到）
    - PTPQdrant 子类覆写 SDK 两处默认行为：
      1) _document_from_point：SDK 默认只从 payload["metadata"] 嵌套键取元数据，
         本项目 payload 为扁平结构 → 覆写为取全部字段（剔除文本字段），
         检索结果 metadata 才带 asset_id/asset_type/chunk_index（引用构造依赖）
      2) _select_relevance_score_fn：Qdrant COSINE 返回相似度（越大越相似），
         SDK 基类按"距离"语义做 1-score 反转 → 覆写为恒等映射，
         score_threshold 即"相似度 ≥ 阈值"（SRS FR-05）
    - embedding 注入 get_embeddings()（检索时 embed_query；未配置 → FakeEmbeddings）
    - point_id 算法共享 embedding_client.point_id_for（检索侧无需传入）

    validate_collection_config=False：跳过构造期连 Qdrant 的配置校验（collection
    由 ensure_collection 启动期保证），构造保持离线可跑。
    """
    global _lc_store
    if _lc_store is not None:
        return _lc_store

    from langchain_core.documents import Document
    # 注意：langchain_qdrant.Qdrant 是旧版兼容 shim（签名 embeddings 复数 +
    # distance_strategy 字符串），新版类是 qdrant.QdrantVectorStore（embedding 单数）
    from langchain_qdrant.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient

    class PTPQdrant(QdrantVectorStore):
        """适配本项目扁平 payload 与 COSINE 相似度语义的 Qdrant 子类。"""

        @classmethod
        def _document_from_point(
            cls, scored_point, collection_name: str,
            content_payload_key: str, metadata_payload_key: str,
        ) -> Document:
            payload = scored_point.payload or {}
            metadata = {k: v for k, v in payload.items() if k != content_payload_key}
            metadata["_id"] = scored_point.id
            metadata["_collection_name"] = collection_name
            return Document(
                page_content=payload.get(content_payload_key, ""),
                metadata=metadata,
            )

        def _select_relevance_score_fn(self):
            # Qdrant COSINE score 是相似度（1=最相似），恒等映射保持阈值语义
            return lambda score: score

    # 复用 QdrantVectorStore 已建的 collection 配置（保证同名 + 同维度）
    base = get_vector_store()
    # 检索端 embedding：get_embeddings() 未配置时返回 FakeEmbeddings 降级（SRS 1.5.4）
    from app.services.embedding_client import get_embeddings

    # langchain_qdrant 1.x 检索方法（similarity_search_with_score 等）均为同步实现，
    # 直接调 self.client.query_points(...).points（同步风格）；若传入 AsyncQdrantClient，
    # query_points 返回协程，.points 访问协程对象会报
    # AttributeError: 'coroutine' object has no attribute 'points'。
    # 因此检索端必须用同步 QdrantClient；VectorStore 基类的 asimilarity_search_*
    # 通过 run_in_executor 把同步调用放到线程池，不会阻塞事件循环。
    # 入库侧（QdrantVectorStore）仍用 AsyncQdrantClient，两 client 共享同一 Qdrant 服务实例。
    settings = get_settings()
    sync_client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=10,
    )
    _lc_store = PTPQdrant(
        client=sync_client,
        collection_name=base._collection,
        embedding=get_embeddings(),
        vector_name="embedding",
        content_payload_key="text_chunk",
        metadata_payload_key="payload",  # 未用：子类覆写 _document_from_point 读扁平 payload
        validate_collection_config=False,
    )
    logger.info(
        f"LangChain Qdrant VectorStore 已就绪：collection={base._collection}, "
        f"content_payload_key=text_chunk, vector_name=embedding"
    )
    return _lc_store


def reset_langchain_vector_store() -> None:
    """测试辅助：清空 LangChain VectorStore 单例。"""
    global _lc_store
    _lc_store = None
