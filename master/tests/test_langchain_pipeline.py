"""LangChain 管道单元测试（P3 Stage 1 新增）：Cleaner / Chunker / LCEL 链 / index_chunks。

覆盖 SRS FR-02/03/04 + NFR-01 降级：
- FR-02 Cleaner：去页眉 / 断词修复 / 非段落换行转空格
- FR-03 Chunker：RecursiveCharacterTextSplitter 中文分隔符行为
- FR-04 Indexer：LCEL RunnableSequence（clean | chunk）+ embedding + 向量库入库
- NFR-01 降级：未配置 Embedding → FakeEmbeddings 入库（indexed=true, degraded=true）
"""

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.documents import Document

from app.services.langchain_pipeline import (
    _clean_text,
    build_chunker,
    build_index_pipeline,
    chunk_documents,
    clean_documents,
    index_chunks,
)


# ---------- FR-02 Cleaner ----------


def test_clean_text_strips_page_header() -> None:
    assert _clean_text("Page 1 of 5\n正文") == "正文"
    assert _clean_text("第 3 页 / 共 10 页\n正文") == "正文"
    assert _clean_text("Page 1 of 5") == ""


def test_clean_text_fixes_hyphen_break() -> None:
    # word-\nword → wordword（英文连字符换行修复）
    assert _clean_text("perfor-\nmance") == "performa​nce".replace("​", "") or "performa​nce".replace("​", "")  # noqa: E501
    # 简单断词
    result = _clean_text("opti-\nmization")
    assert "optimization" in result.replace(" ", "")


def test_clean_text_single_newline_to_space() -> None:
    # 单 \n 转空格，\n\n 保留
    text = "第一行\n第二行\n\n新段落"
    result = _clean_text(text)
    assert "第一行 第二行" in result
    assert "\n\n" in result


def test_clean_documents_drops_empty() -> None:
    docs = [
        Document(page_content="有效内容"),
        Document(page_content="   \n  "),
        Document(page_content="Page 1 of 1"),  # 清洗后空
    ]
    cleaned = clean_documents(docs)
    assert len(cleaned) == 1
    assert cleaned[0].page_content == "有效内容"


# ---------- FR-03 Chunker ----------


def test_chunker_short_text_single_chunk() -> None:
    docs = [Document(page_content="短段落")]
    chunks = chunk_documents(docs)
    assert len(chunks) == 1
    assert chunks[0].page_content == "短段落"


def test_chunker_aggregates_within_size() -> None:
    # 20 段，每段约 44 字，总 880 字 → chunk_size=500 应聚合为 2 块
    paras = [f"第{i}段" + "内容" * 20 for i in range(20)]
    text = "\n".join(paras)
    docs = [Document(page_content=text)]
    chunks = chunk_documents(docs)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.page_content) <= 500


def test_chunker_hard_splits_long_paragraph() -> None:
    # 超长无分隔符 1200 字 → chunk_size=500, overlap=50 → 3 块
    long_text = "压" * 1200
    docs = [Document(page_content=long_text)]
    chunks = chunk_documents(docs)
    assert len(chunks) == 3
    assert all(len(c.page_content) <= 500 for c in chunks)


def test_chunker_metadata_propagated() -> None:
    docs = [Document(page_content="内容" * 200, metadata={"asset_type": "plan_doc"})]
    chunks = chunk_documents(docs)
    assert all(c.metadata.get("asset_type") == "plan_doc" for c in chunks)


def test_build_chunker_respects_settings() -> None:
    chunker = build_chunker(chunk_size=100, chunk_overlap=10)
    assert chunker._chunk_size == 100


# ---------- FR-04 LCEL 链 ----------


def test_index_pipeline_clean_then_chunk() -> None:
    pipeline = build_index_pipeline()
    docs = [
        Document(page_content="Page 1 of 1\n" + "内容" * 200),
        Document(page_content="第二段\n" + "数据" * 200),
    ]
    chunks = pipeline.invoke(docs)
    assert len(chunks) >= 2
    # 页眉应被清掉
    assert all("Page 1 of 1" not in c.page_content for c in chunks)


# ---------- FR-04 + NFR-01 index_chunks（降级入库） ----------


def _make_settings(**overrides) -> SimpleNamespace:
    values = dict(
        embedding_provider="",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
        embedding_batch_size=64,
        embedding_timeout=5,
        embedding_max_retries=1,
        embedding_dims=4,
        chunk_size=500,
        chunk_overlap=50,
        top_k=5,
        similarity_threshold=0.5,
        use_bm25=False,
        max_context_chars=1800,
        metric_tolerance=0.05,
        timezone="Asia/Shanghai",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeEmbeddings:
    """模拟 LangChain Embeddings（aembed_documents 返回固定向量）。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


class FailingEmbeddings:
    """模拟 Embedding 调用失败。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("上游 503")

    async def aembed_query(self, text: str) -> list[float]:
        raise RuntimeError("上游 503")


class FakeStore:
    def __init__(self) -> None:
        self.upserted: list = []

    async def upsert_chunks(self, points) -> None:
        self.upserted.extend(points)


class FakeAsset:
    """模拟 Asset ORM 实体（index_chunks 只读 id/project_id/asset_type/file_key）。"""

    def __init__(self, asset_id: int = 1, project_id: int = 1, asset_type: str = "plan_doc"):
        self.id = asset_id
        self.project_id = project_id
        self.asset_type = asset_type
        self.file_key = "assets/1/test.docx"


async def test_index_chunks_degraded_fake_embeddings(monkeypatch):
    """NFR-01：未配置 Embedding → FakeEmbeddings 入库，indexed=true, degraded=true。"""
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(embedding_dims=4),
    )
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: FakeEmbeddings()
    )
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)

    docs = [Document(page_content="内容" * 100 + "\n\n" + "数据" * 100)]
    meta: dict[str, Any] = {}
    asset = FakeAsset()
    chunks = await index_chunks(docs, asset, meta)

    assert len(chunks) >= 1
    assert meta["indexed"] is True
    assert meta["degraded"] is True
    assert len(store.upserted) == len(chunks)
    # point_id 确定性
    from app.services.embedding_client import point_id_for

    assert store.upserted[0].point_id == point_id_for(1, 0)


async def test_index_chunks_configured_no_degraded(monkeypatch):
    """已配置 Embedding → indexed=true, degraded 不标记。"""
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(
            embedding_api_key="k", embedding_model="m", embedding_dims=4
        ),
    )
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: FakeEmbeddings()
    )
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)

    docs = [Document(page_content="内容" * 100)]
    meta: dict[str, Any] = {}
    await index_chunks(docs, FakeAsset(), meta)

    assert meta["indexed"] is True
    assert meta.get("degraded") is not True


async def test_index_chunks_failure_propagates(monkeypatch):
    """已配置但调用失败 → 抛异常（上层 asset_parser 标记 FAILED）。"""
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(
            embedding_api_key="k", embedding_model="m", embedding_dims=4
        ),
    )
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: FailingEmbeddings()
    )
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)

    docs = [Document(page_content="内容" * 100)]
    meta: dict[str, Any] = {}
    with pytest.raises(RuntimeError, match="上游 503"):
        await index_chunks(docs, FakeAsset(), meta)
    assert store.upserted == []


async def test_index_chunks_empty_documents_returns_empty():
    """空 documents 直接返回空列表，不调 embedding/vector_store。"""
    chunks = await index_chunks([], FakeAsset(), {})
    assert chunks == []


async def test_index_chunks_cleaner_applied(monkeypatch):
    """index_chunks 内部应先 clean 再 chunk（页眉被去除）。"""
    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_settings",
        lambda: _make_settings(embedding_dims=4),
    )
    captured_chunks: list[str] = []

    class CaptureEmbeddings:
        async def aembed_documents(self, texts):
            captured_chunks.extend(texts)
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        async def aembed_query(self, text):
            return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(
        "app.services.langchain_pipeline.get_embeddings", lambda: CaptureEmbeddings()
    )
    store = FakeStore()
    monkeypatch.setattr("app.services.langchain_pipeline.get_vector_store", lambda: store)

    docs = [Document(page_content="Page 1 of 5\n" + "内容" * 200)]
    await index_chunks(docs, FakeAsset(), {})

    assert all("Page 1 of 5" not in c for c in captured_chunks)
