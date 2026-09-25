"""LangChain LCEL 管道（FR-02/03/04，SRS 1.5.3）。

FR-02 Cleaner：RunnableLambda 对 list[Document] 变换（去页眉/断词修复/非段落换行转空格）
FR-03 Chunker：RecursiveCharacterTextSplitter（中文分隔符优先级：段落 > 换行 > 句号 > 逗号 > 空格）
FR-04 Indexer：LCEL RunnableSequence（clean | chunk）+ embedding + 向量库入库

降级（NFR-01，SRS 1.5.4）：未配置 Embedding 时用 FakeEmbeddings 入库
（indexed=true, degraded=true），保证端到端链路不中断。

依赖方向：
- langchain_pipeline → embedding_client（get_embeddings + point_id_for）
- langchain_pipeline → vector_store（get_vector_store + ChunkDoc）
- asset_parser → langchain_pipeline（index_chunks）
- 无循环依赖
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import get_settings
from app.services.embedding_client import get_embeddings, point_id_for
from app.services.vector_store import ChunkDoc, get_vector_store

if TYPE_CHECKING:
    from app.models.asset import Asset


# ---------- FR-02 Cleaner ----------

# "Page x of y" / "第 x 页 / 共 y 页" 页眉
_PAGE_HEADER_RE = re.compile(
    r"(?:Page\s+\d+\s+of\s+\d+|第\s*\d+\s*页\s*[/,／]\s*共\s*\d+\s*页)", re.IGNORECASE
)
# 断词修复：word-\nword → wordword（英文连字符换行）
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\n(\w)")
# 非段落换行（单 \n，前后非 \n）转空格，保留段落分隔 \n\n
# 实现策略：先保护 \n\n → \x00，再单 \n → 空格，最后恢复 \x00 → \n\n


def _clean_text(text: str) -> str:
    """单文本清洗：去页眉 → 断词修复 → 非段落换行转空格。"""
    if not text:
        return ""
    # 1. 去页眉
    text = _PAGE_HEADER_RE.sub("", text)
    # 2. 断词修复
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    # 3. 非段落换行转空格（保留 \n\n）
    text = text.replace("\n\n", "\x00")
    # 单 \n（不跟另一个 \n）转空格
    text = re.sub(r"\n(?!\x00)", " ", text)
    text = text.replace("\x00", "\n\n")
    return text.strip()


def clean_documents(docs: list[Document]) -> list[Document]:
    """FR-02 Cleaner：对 list[Document] 变换。

    - 去页眉 "Page x of y" / "第 x 页 / 共 y 页"
    - 断词修复（word-\\n → word）
    - 非段落换行转空格（保留 \\n\\n 段落分隔）
    - 清洗后空内容丢弃
    """
    cleaned: list[Document] = []
    for doc in docs:
        content = _clean_text(doc.page_content)
        if content:
            cleaned.append(Document(page_content=content, metadata=dict(doc.metadata)))
    return cleaned


def build_cleaner() -> RunnableLambda:
    """FR-02 Cleaner 作为 LCEL Runnable。"""
    return RunnableLambda(clean_documents)


# ---------- FR-03 Chunker ----------

# 中文分隔符优先级：段落 > 换行 > 句号 > 感叹 > 问号 > 分号 > 逗号 > 空格 > 空串
_CN_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


def build_chunker(
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> RecursiveCharacterTextSplitter:
    """FR-03 Chunker：RecursiveCharacterTextSplitter（中文分隔符）。"""
    settings = get_settings()
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size or settings.chunk_size,
        chunk_overlap=chunk_overlap or settings.chunk_overlap,
        separators=_CN_SEPARATORS,
        is_separator_regex=False,
    )


def chunk_documents(docs: list[Document]) -> list[Document]:
    """FR-03 Chunker：对 list[Document] 切分（合并 page_content 后用 split_text）。

    RecursiveCharacterTextSplitter.split_documents 逐 Document 切分不跨 Document 聚合，
    会导致短段落各自成块。这里合并所有 page_content（用 \\n\\n 连接保留段落分隔）
    后用 split_text 切分，metadata 取首 Document 的（所有 chunk 共用 asset_type/source_ref
    等溯源字段，页级溯源待 Stage 2 细化）。
    """
    if not docs:
        return []
    chunker = build_chunker()
    combined_text = "\n\n".join(d.page_content for d in docs if d.page_content.strip())
    if not combined_text:
        return []
    base_meta = dict(docs[0].metadata)
    chunks = chunker.split_text(combined_text)
    return [Document(page_content=c, metadata=dict(base_meta)) for c in chunks]


def build_splitter_runnable() -> RunnableLambda:
    """FR-03 Chunker 作为 LCEL Runnable。"""
    return RunnableLambda(chunk_documents)


# ---------- FR-04 Indexer（LCEL 链 + 入库） ----------


def build_index_pipeline():
    """LCEL RunnableSequence: clean | chunk（仅文本变换，不含 embedding/入库）。

    embedding + 入库在 `index_chunks` 中单独处理（需 asset 上下文 + async）。
    """
    return build_cleaner() | build_splitter_runnable()


async def index_chunks(
    documents: list[Document],
    asset: "Asset",
    meta: dict,
) -> list[str]:
    """FR-04 Indexer：clean → chunk → embedding → 向量库入库。

    - LCEL 链 clean | chunk 得到切片 Document
    - get_embeddings() 工厂返回 LangChain Embeddings
      （已配置 → OpenAIEmbeddings；未配置 → FakeEmbeddings 降级，SRS 1.5.4）
    - point_id 用 `crc32(f"{asset_id}:{chunk_index}")` 与现有 QdrantVectorStore 共享
    - 降级入库：indexed=true, degraded=true（不再跳过）
    - 已配置但调用失败抛 EmbeddingError → 上层 asset_parser 标记 FAILED

    返回切片文本列表（供 meta["chunks"] 计数）。
    """
    if not documents:
        return []

    # LCEL 链：clean → chunk
    pipeline = build_index_pipeline()
    chunks = pipeline.invoke(documents)
    # 过滤空/纯空白切片：DashScope 拒绝空 input（400 Range of input length），
    # 也避免脏切片污染向量库
    chunk_texts = [
        d.page_content for d in chunks if d.page_content and d.page_content.strip()
    ]
    if not chunk_texts:
        return []

    # FR-04 embedding（get_embeddings 内部处理降级）
    embeddings = get_embeddings()
    vectors = await embeddings.aembed_documents(chunk_texts)

    # FR-04 入库（共享 collection + point_id 算法）
    store = get_vector_store()
    docs = [
        ChunkDoc(
            point_id=point_id_for(asset.id, index),
            asset_id=asset.id,
            project_id=asset.project_id,
            chunk_index=index,
            text_chunk=text,
            embedding=vector,
            asset_type=asset.asset_type,
            source_type="asset",
            source_ref=asset.file_key,
        )
        for index, (text, vector) in enumerate(zip(chunk_texts, vectors, strict=True))
    ]
    await store.upsert_chunks(docs)
    meta["indexed"] = True
    # 降级标记：get_embeddings 返回 FakeEmbeddings 时记 degraded=true
    settings = get_settings()
    if not (settings.embedding_api_key and settings.embedding_model):
        meta["degraded"] = True
    return chunk_texts
