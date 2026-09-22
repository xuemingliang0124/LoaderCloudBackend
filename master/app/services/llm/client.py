"""LLM 引擎 + 统一输出解析（FR-07/FR-09，SRS 附录 A）。

职责（P3 Stage 2）：
- `AnswerOutput`：统一输出 Pydantic 模型（answer/citations/used_metrics/confidence/notes）
- `get_llm()`：LangChain ChatModel 工厂（单例）
  - 已配置 LLM_API_KEY + LLM_MODEL + base_url → `ChatOpenAI`（OpenAI 兼容
    /chat/completions，对接智谱/阿里）
  - 未配置 → `FakeListChatModel` 降级（SRS 1.5.4：返回兜底响应，不抛异常）
- `parse_answer()`：`PydanticOutputParser` 解析 + 降级回退
  - JSON 解析失败（含前后缀垃圾文本）→ `_fallback_payload`
  - citations 为空 → 兜底填充检索 Top-1 引用（SRS FR-07 异常处理）
- `format_citation()`：引用标识 `asset_type:asset_id:chunk_index`（SRS 5.3）

关键约束（实施方案风险表）：FakeListChatModel 不触发 tool_calls（bind_tools 抛
NotImplementedError），orchestrator 据此判定走纯 RAG 链路径；Mock 路径只走
fallback，不依赖 Function Calling。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import PydanticOutputParser
from loguru import logger
from pydantic import BaseModel, Field

from app.core.config import get_settings

if TYPE_CHECKING:
    from langchain_core.documents import Document


class AnswerOutput(BaseModel):
    """FR-09 统一输出格式（SRS 附录 A JSON 结构，所有 LLM 接口返回此结构）。"""

    answer: str
    citations: list[str] = Field(default_factory=list)
    used_metrics: list[str] | None = None
    confidence: float = 0.5
    notes: str = ""


# FakeListChatModel 降级时的预设响应（合法 AnswerOutput JSON）。
# citations 留空：由 parse_answer 兜底填充检索 Top-1（SRS FR-07：Mock 降级时
# 引用取检索 Top-1、notes 标注降级原因）
FALLBACK_RESPONSE = json.dumps(
    {
        "answer": "（降级响应）LLM 未配置，当前为 Mock 模式，请参考知识库检索结果。",
        "citations": [],
        "used_metrics": None,
        "confidence": 0.5,
        "notes": "LLM 未配置，FakeListChatModel 降级",
    },
    ensure_ascii=False,
)

_PARSER = PydanticOutputParser(pydantic_object=AnswerOutput)


def get_answer_parser() -> PydanticOutputParser:
    """AnswerOutput 解析器（PydanticOutputParser，失败抛 OutputParserException）。"""
    return _PARSER


def format_citation(doc: "Document") -> str:
    """从 Document.metadata 构造引用标识 `asset_type:asset_id:chunk_index`。

    SRS 5.3：citations 字符串不含方括号（括号是文档记法），如 "plan_doc:12:chunk_3"。
    metadata 缺字段时用占位值兜底，保证引用格式合规率（SRS 8.1 KPI）。
    """
    meta = getattr(doc, "metadata", None) or {}
    asset_type = meta.get("asset_type") or "unknown"
    asset_id = meta["asset_id"] if meta.get("asset_id") is not None else "unknown"
    chunk_index = meta["chunk_index"] if meta.get("chunk_index") is not None else 0
    return f"{asset_type}:{asset_id}:chunk_{chunk_index}"


# ---------- LLM 工厂（FR-07 + NFR-01 降级） ----------

_llm: object | None = None


def get_llm():
    """工厂：返回 LangChain ChatModel（单例）。

    - 已配置 LLM_API_KEY + LLM_MODEL + base_url（显式或 provider 默认）→
      `langchain_openai.ChatOpenAI`
    - 未配置 → `FakeListChatModel(responses=[FALLBACK_RESPONSE])` 降级
      （SRS 1.5.4：返回兜底响应，不抛异常，端到端链路不中断）

    判定范式与 embedding_client.get_embeddings 一致：base_url 用
    llm_base_url_resolved（显式配置优先 provider 默认值）。
    """
    global _llm
    if _llm is not None:
        return _llm

    settings = get_settings()
    if settings.llm_api_key and settings.llm_model and settings.llm_base_url_resolved:
        from langchain_openai import ChatOpenAI

        # ChatOpenAI 字段：model / api_key / base_url / temperature / timeout
        _llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url_resolved,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
        )
        logger.info(
            f"LLM 已配置：model={settings.llm_model}, "
            f"base_url={settings.llm_base_url_resolved}"
        )
        return _llm

    _llm = FakeListChatModel(responses=[FALLBACK_RESPONSE])
    logger.warning("LLM 未配置，降级使用 FakeListChatModel（返回兜底响应）")
    return _llm


def reset_llm() -> None:
    """测试辅助：清空 LLM 单例。"""
    global _llm
    _llm = None


# ---------- 输出解析 + 降级回退（FR-07 异常处理 / FR-09 字段补齐） ----------


def normalize_json(raw: str) -> str:
    """提取 raw 中第一个 { 到最后一个 } 的子串（容忍前后缀垃圾文本）。

    markdown 围栏（```json ...```）由 PydanticOutputParser 自行剥离，
    此处仅处理解析器也搞不定的"前后缀污染"场景。
    """
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _fallback_payload(docs: list["Document"], reason: str) -> AnswerOutput:
    """兜底 payload（SRS FR-07）：引用取检索 Top-1，notes 标注降级原因。

    空库（docs 为空）时 citations 为空列表、answer 给出明确降级说明，
    保证 FR-09 五字段完整且不抛异常（NFR-02 空库鲁棒性）。
    """
    if docs:
        top = docs[0]
        return AnswerOutput(
            answer=(
                "（降级回答）未能获得 LLM 有效响应。"
                f"以下为知识库中最相关的内容：\n{top.page_content[:500]}"
            ),
            citations=[format_citation(top)],
            used_metrics=None,
            confidence=0.5,
            notes=reason,
        )
    return AnswerOutput(
        answer="（降级回答）LLM 未配置或响应不可用，且知识库中暂无相关内容。",
        citations=[],
        used_metrics=None,
        confidence=0.5,
        notes=reason,
    )


def parse_answer(raw: str, docs: list["Document"], reason: str = "") -> AnswerOutput:
    """解析 LLM 原始输出 → AnswerOutput；失败走兜底（FR-07/FR-09）。

    - 解析失败（OutputParserException，含非法 JSON/字段类型错误）→ `_fallback_payload`
    - citations 为空 → 兜底填充检索 Top-1 引用，notes 追加说明（SRS FR-07）
    """
    try:
        output = _PARSER.parse(normalize_json(raw))
    except OutputParserException:
        fallback_reason = reason or "LLM 响应 JSON 解析失败，已降级"
        logger.warning(f"AnswerOutput 解析失败，走兜底: {raw[:120]!r}")
        return _fallback_payload(docs, fallback_reason)
    if not output.citations and docs:
        output.citations = [format_citation(docs[0])]
        extra = "citations 为空，已用检索 Top-1 兜底"
        output.notes = "; ".join(x for x in (output.notes, extra) if x)
    return output
