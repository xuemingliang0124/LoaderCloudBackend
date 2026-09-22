"""LangChain BaseLoader 适配器（FR-01，SRS 1.5.3）。

把现有 `asset_parser.parse_docx/parse_pdf/parse_xlsx` 包装成 LangChain `BaseLoader`
子类（适配器模式 B，底层解析器不重写）。

设计：
- `iter_documents_from_parsed(parsed, include_tables)` 模块函数：从 `ParsedDoc` 生成
  LangChain `Document`，供 asset_parser 文本路径直接调用（避免重复解析 bytes）
- 3 个 `BaseLoader` 子类：供 Stage 2 独立加载场景用（如知识库补充、chat 上传 PDF）
  内部延迟 import asset_parser 的解析函数，打破循环依赖
- `table_text_paragraphs(tables)`：从 asset_parser 迁入的公共工具（表格→伪段落文本）
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from langchain_core.documents import Document
from langchain_core.document_loaders import BaseLoader

if TYPE_CHECKING:
    from app.services.asset_parser import ParsedDoc


def table_text_paragraphs(tables: list[list[list[str]]]) -> list[str]:
    """表格 → 伪段落文本（供非清单类文档的表格内容进向量库）。

    从 asset_parser 迁入此模块，打破 asset_parser ↔ langchain_loader 循环依赖。
    """
    paragraphs: list[str] = []
    for table in tables:
        lines = [" | ".join(cells) for cells in table if cells]
        if lines:
            paragraphs.append("\n".join(lines))
    return paragraphs


def iter_documents_from_parsed(
    parsed: "ParsedDoc",
    include_tables: bool = True,
    asset_type: str = "",
    source_ref: str = "",
) -> Iterator[Document]:
    """从 ParsedDoc 生成 LangChain Document（asset_parser 文本路径直接调用）。

    - paragraphs → Document（每段一个）
    - include_tables=True 时，表格→伪段落文本→Document（非清单类资产用）
    - metadata 记录 asset_type / source_ref，供检索阶段溯源
    """
    base_metadata = {
        "asset_type": asset_type,
        "source_type": "asset",
        "source_ref": source_ref,
    }
    for para in parsed.paragraphs:
        yield Document(page_content=para, metadata=dict(base_metadata))
    if include_tables:
        for table_text in table_text_paragraphs(parsed.tables):
            yield Document(page_content=table_text, metadata=dict(base_metadata))


class DocxAssetLoader(BaseLoader):
    """FR-01 Docx Loader：包装 `asset_parser.parse_docx`。

    供 Stage 2 独立加载场景用；asset_parser._parse_asset 走
    `iter_documents_from_parsed` 避免重复解析。
    """

    def __init__(self, data: bytes, asset_type: str = "", source_ref: str = "") -> None:
        self.data = data
        self._asset_type = asset_type
        self._source_ref = source_ref

    def lazy_load(self) -> Iterator[Document]:
        from app.services.asset_parser import parse_docx

        parsed = parse_docx(self.data)
        yield from iter_documents_from_parsed(
            parsed, include_tables=True, asset_type=self._asset_type,
            source_ref=self._source_ref,
        )


class PdfAssetLoader(BaseLoader):
    """FR-01 PDF Loader：包装 `asset_parser.parse_pdf`，每页文本合并为单 Document。

    注：parse_pdf 已按页提取 paragraphs，这里每页一个 Document（page 入 metadata）。
    """

    def __init__(self, data: bytes, asset_type: str = "", source_ref: str = "") -> None:
        self.data = data
        self._asset_type = asset_type
        self._source_ref = source_ref

    def lazy_load(self) -> Iterator[Document]:
        from app.services.asset_parser import parse_pdf

        parsed = parse_pdf(self.data)
        # pdf 按页提取 paragraphs，每页一个 Document（page index 入 metadata）
        for page_index, page_text in enumerate(parsed.paragraphs, start=1):
            yield Document(
                page_content=page_text,
                metadata={
                    "asset_type": self._asset_type,
                    "source_type": "asset",
                    "source_ref": self._source_ref,
                    "page": page_index,
                },
            )
        # pdf 表格也进向量库（非清单类资产）
        for table_text in table_text_paragraphs(parsed.tables):
            yield Document(
                page_content=table_text,
                metadata={
                    "asset_type": self._asset_type,
                    "source_type": "asset",
                    "source_ref": self._source_ref,
                    "page": 0,
                },
            )


class ExcelInventoryLoader(BaseLoader):
    """FR-01 Excel Loader：包装 `asset_parser.parse_xlsx`。

    清单类资产表格已结构化抽取（environments/transactions），不再入向量库；
    非清单类 xlsx 走 `iter_documents_from_parsed(include_tables=True)`。
    """

    def __init__(
        self, data: bytes, asset_type: str = "", source_ref: str = "",
        include_tables: bool = True,
    ) -> None:
        self.data = data
        self._asset_type = asset_type
        self._source_ref = source_ref
        self._include_tables = include_tables

    def lazy_load(self) -> Iterator[Document]:
        from app.services.asset_parser import parse_xlsx

        parsed = parse_xlsx(self.data)
        yield from iter_documents_from_parsed(
            parsed, include_tables=self._include_tables,
            asset_type=self._asset_type, source_ref=self._source_ref,
        )
