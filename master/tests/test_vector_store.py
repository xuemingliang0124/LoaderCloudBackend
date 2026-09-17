"""VectorStore 抽象层契约测试。

风格参照 test_runs.py 的 monkeypatch 范式：
- 数据类 + 工厂分发逻辑：纯 Python，不依赖 qdrant-client 是否安装
- QdrantVectorStore 行为测试：未安装 qdrant-client 时跳过（Docker 构建期
  会装，CI 完整覆盖）
- 不连真实 Qdrant 容器，用 AsyncMock 替换 _client 做行为验证
"""

from unittest.mock import AsyncMock

import pytest

from app.services.vector_store import (
    ChunkDoc,
    ChunkHit,
    QdrantVectorStore,
    get_vector_store,
    reset_vector_store,
)

# qdrant-client 在 QdrantVectorStore.__init__ 内局部 import。
# 顶部 importorskip 会让数据类测试也被跳过，故放此处仅用于行为测试组。
try:
    from qdrant_client.http import models as qmodels
except ImportError:  # pragma: no cover
    qmodels = None  # type: ignore[assignment]

_QDRANT_REASON = "qdrant-client 未安装（Docker 构建期会装，CI 完整覆盖）"
requires_qdrant = pytest.mark.skipif(qmodels is None, reason=_QDRANT_REASON)


# ---- 数据类（不依赖 qdrant-client SDK）----


def test_chunk_doc_defaults() -> None:
    """ChunkDoc 必填字段齐全，created_at 默认 utcnow（业务层不传也能用）。"""
    chunk = ChunkDoc(
        point_id=1,
        asset_id=10,
        project_id=100,
        chunk_index=0,
        text_chunk="hello",
        embedding=[0.1] * 1024,
    )
    assert chunk.asset_type == ""
    assert chunk.source_type == ""
    assert chunk.source_ref == ""
    assert chunk.created_at is not None


def test_chunk_hit_payload_dict() -> None:
    """ChunkHit.payload 是 dict（业务层按 key 取值，不耦合 Store 实现细节）。"""
    hit = ChunkHit(point_id=1, score=0.9, payload={"text_chunk": "x"})
    assert hit.payload["text_chunk"] == "x"


# ---- 工厂分发（不依赖 qdrant-client SDK 的部分）----


def test_get_vector_store_unsupported_provider() -> None:
    """未实现的 provider 必须抛 NotImplementedError，避免静默走错分支。

    vector_provider=milvus 不构造任何 Store，不触发 SDK import。
    """
    reset_vector_store()
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.vector_provider
    settings.vector_provider = "milvus"
    try:
        with pytest.raises(NotImplementedError, match="milvus"):
            get_vector_store()
    finally:
        settings.vector_provider = original
        reset_vector_store()


# ---- 工厂单例 / Qdrant 行为：依赖 qdrant-client SDK ----
# QdrantVectorStore.__init__ 内局部 import qdrant_client；未安装时跳过。
# Docker 构建期会装 requirements.txt 的 qdrant-client>=1.11，CI 完整覆盖。


def _make_store_with_mock_client() -> tuple[QdrantVectorStore, AsyncMock]:
    """构造 QdrantVectorStore 但绕过 __init__ 的 SDK 客户端实例化。

    直接 __new__ 跳过 __init__，注入 AsyncMock 替换的 fake client；
    _qmodels 用真实 SDK models（已 importorskip 通过）。
    """
    store = QdrantVectorStore.__new__(QdrantVectorStore)
    store._collection = "pt_knowledge"
    store._dims = 1024
    store._qmodels = qmodels
    store._client = AsyncMock()
    return store, store._client


@requires_qdrant
def test_get_vector_store_singleton() -> None:
    """同一 provider 多次调用返回同一实例（避免每次构造新 client）。"""
    reset_vector_store()
    try:
        s1 = get_vector_store()
        s2 = get_vector_store()
        assert s1 is s2
    finally:
        reset_vector_store()


@requires_qdrant
def test_reset_vector_store_clears_singleton() -> None:
    """reset 后再次获取得到新实例（测试夹具必须能重置单例）。"""
    reset_vector_store()
    s1 = get_vector_store()
    reset_vector_store()
    s2 = get_vector_store()
    assert s1 is not s2
    reset_vector_store()


# ---- QdrantVectorStore 行为 ----


@requires_qdrant
async def test_ensure_collection_creates_when_missing() -> None:
    """collection 不存在时调 create_collection（幂等创建）。"""
    store, client = _make_store_with_mock_client()
    client.get_collections = AsyncMock(
        return_value=qmodels.CollectionsResponse(collections=[])
    )
    client.create_collection = AsyncMock()

    await store.ensure_collection()

    client.create_collection.assert_awaited_once()
    call = client.create_collection.await_args
    vc = call.kwargs["vectors_config"]
    assert vc.size == 1024
    assert vc.distance == qmodels.Distance.COSINE
    assert call.kwargs["collection_name"] == "pt_knowledge"


@requires_qdrant
async def test_ensure_collection_skips_when_exists() -> None:
    """collection 已存在时不调 create_collection（保护存量数据）。"""
    store, client = _make_store_with_mock_client()
    fake = qmodels.CollectionsResponse(
        collections=[qmodels.CollectionDescription(name="pt_knowledge")]
    )
    client.get_collections = AsyncMock(return_value=fake)
    client.create_collection = AsyncMock()

    await store.ensure_collection()

    client.create_collection.assert_not_awaited()


@requires_qdrant
async def test_upsert_chunks_empty_list_noop() -> None:
    """空列表直接返回（避免 Qdrant upsert 报错；业务层 batch 末尾可能为空）。"""
    store, client = _make_store_with_mock_client()
    client.upsert = AsyncMock()

    await store.upsert_chunks([])

    client.upsert.assert_not_awaited()


@requires_qdrant
async def test_upsert_chunks_writes_all_with_payload() -> None:
    """非空列表逐条构造 PointStruct，vector 字段固定名 embedding，payload 含所有元数据。"""
    store, client = _make_store_with_mock_client()
    client.upsert = AsyncMock()

    chunks = [
        ChunkDoc(
            point_id=i,
            asset_id=10,
            project_id=100,
            chunk_index=i,
            text_chunk=f"chunk-{i}",
            embedding=[0.1] * 1024,
            asset_type="sla_doc",
            source_type="domain_doc",
            source_ref="ref.md",
        )
        for i in range(3)
    ]
    await store.upsert_chunks(chunks)

    client.upsert.assert_awaited_once()
    call = client.upsert.await_args
    assert call.kwargs["collection_name"] == "pt_knowledge"
    points = call.kwargs["points"]
    assert len(points) == 3
    p0 = points[0]
    assert p0.id == 0
    # vector 字段固定名 embedding（search 端必须知道从哪个字段取向量）
    assert "embedding" in p0.vector
    assert len(p0.vector["embedding"]) == 1024
    # payload 字段齐全（search 端按 key 取值）
    assert p0.payload["asset_id"] == 10
    assert p0.payload["project_id"] == 100
    assert p0.payload["asset_type"] == "sla_doc"
    assert p0.payload["text_chunk"] == "chunk-0"
    assert p0.payload["source_ref"] == "ref.md"
    assert p0.payload["chunk_index"] == 0


@requires_qdrant
async def test_search_forces_project_id_filter() -> None:
    """多租户隔离：project_id 必过滤，filters 为额外条件，不能绕过 project_id。"""
    store, client = _make_store_with_mock_client()
    client.search = AsyncMock(
        return_value=[
            qmodels.ScoredPoint(
                id=1, score=0.9, version=0, payload={"text_chunk": "hit-1"}
            )
        ]
    )

    await store.search(
        project_id=100,
        query_vec=[0.1] * 1024,
        top_k=5,
        filters={"asset_type": "sla_doc"},
    )

    client.search.assert_awaited_once()
    call = client.search.await_args
    assert call.kwargs["collection_name"] == "pt_knowledge"
    assert call.kwargs["limit"] == 5
    flt = call.kwargs["query_filter"]
    keys = {c.key for c in flt.must}
    assert "project_id" in keys
    assert "asset_type" in keys


@requires_qdrant
async def test_search_translates_dict_filters_to_field_conditions() -> None:
    """filters dict 的每个 k/v 翻译成 FieldCondition（迁移 Milvus 时只改这部分翻译逻辑）。"""
    store, client = _make_store_with_mock_client()
    client.search = AsyncMock(return_value=[])

    await store.search(
        project_id=200,
        query_vec=[0.2] * 1024,
        top_k=3,
        filters={"source_type": "report", "asset_type": "plan_doc"},
    )

    call = client.search.await_args
    flt = call.kwargs["query_filter"]
    keys = {c.key for c in flt.must}
    assert keys == {"project_id", "source_type", "asset_type"}


@requires_qdrant
async def test_search_no_extra_filters_still_filters_project() -> None:
    """filters=None 时也必须过滤 project_id（隔离底线）。"""
    store, client = _make_store_with_mock_client()
    client.search = AsyncMock(return_value=[])

    await store.search(project_id=300, query_vec=[0.3] * 1024, top_k=10)

    call = client.search.await_args
    flt = call.kwargs["query_filter"]
    keys = {c.key for c in flt.must}
    assert keys == {"project_id"}


@requires_qdrant
async def test_search_returns_chunk_hits_with_payload() -> None:
    """返回的 ChunkHit 携带 score 与 payload dict（业务层按 key 取值）。"""
    store, client = _make_store_with_mock_client()
    client.search = AsyncMock(
        return_value=[
            qmodels.ScoredPoint(
                id=7, score=0.88, version=0, payload={"text_chunk": "hit-x"}
            ),
            qmodels.ScoredPoint(
                id=9, score=0.55, version=0, payload={"text_chunk": "hit-y"}
            ),
        ]
    )

    hits = await store.search(project_id=1, query_vec=[0.0] * 1024, top_k=2)

    assert len(hits) == 2
    assert hits[0].point_id == 7
    assert hits[0].score == pytest.approx(0.88)
    assert hits[0].payload["text_chunk"] == "hit-x"
    assert hits[1].point_id == 9
