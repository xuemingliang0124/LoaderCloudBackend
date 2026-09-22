"""RAG 检索 + 提示词 + 链编排（FR-05/06/07/08，SRS B.7~B.9）。

- `build_retriever`：`as_retriever(search_type="similarity_score_threshold")` +
  project_id 强制过滤（多租户隔离）；可选 BM25 混检（EnsembleRetriever，
  USE_BM25 默认关闭，语料经 scroll 拉取，失败降级纯向量检索）
- `build_prompt`：ChatPromptTemplate（系统约束/问题改写/证据拼接/引用占位符）
- `run_qa_chain`：检索 → context 拼接（≤ max_context_chars，超长截断）→
  LLM 调用 → 解析/降级
  - Function Calling 路径：llm 支持 bind_tools 且 use_tools=True 时走
    langgraph create_react_agent 工具编排（需真实 LLM，离线不可测）
  - 纯 RAG 路径：FakeListChatModel（Mock 降级）或 use_tools=False 时走
    prompt | llm | StrOutputParser；LLM 调用失败/解析异常 → 兜底 payload
    （SRS 1.5.4 + FR-07：不抛异常，citations 取检索 Top-1）

依赖方向：orchestrator → client（get_llm/parse_answer/fallback）+ tools
（build_tools）+ vector_store（get_langchain_vector_store）；无循环依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.retrievers import BaseRetriever
from loguru import logger

from app.core.config import get_settings
from app.services.llm.client import (
    AnswerOutput,
    _fallback_payload,
    format_citation,
    get_llm,
    parse_answer,
)
from app.services.llm.guardrail import validate_answer
from app.services.llm.tools import build_tools
from app.services.vector_store import get_langchain_vector_store, get_vector_store

if TYPE_CHECKING:
    from langchain_core.retrievers import RetrieverOutput


# FR-06 系统提示词：约束（禁编造）+ 问题改写 + 引用格式说明 + 证据插槽 + 输出 JSON 结构。
# 注意：模板用 str.format 渲染，正文不能出现字面花括号（JSON 结构用文字描述）。
SYSTEM_PROMPT = """你是性能测试平台的智能助手。请严格基于知识库上下文与工具结果回答用户问题，禁止编造任何数值或事实；上下文不足以回答时明确说明。
回答前先将用户口语化问题改写为明确指令再作答。
引用来源时使用引用标识，格式：[资产类型:资产ID:chunk_序号]，例如 [plan_doc:12:chunk_3]；只可引用可用引用标识列表中的条目。

知识库上下文（可能被截断）：
{context}

可用引用标识：{citations}

输出要求：只输出一个 JSON 对象，不要输出 JSON 以外的任何文字。字段定义：
- answer：自然语言回答（字符串，含引用标识）
- citations：引用标识字符串数组
- used_metrics：涉及的指标名数组（tps/p95_ms/error_rate），无则 null
- confidence：置信度浮点数（0-1）
- notes：备注字符串（指标不一致/降级原因等），无则空串"""


# ---------- FR-05 Retriever ----------


class _TopKRetriever(BaseRetriever):
    """EnsembleRetriever 结果截断包装：保证 BM25+向量合并去重后 ≤ top_k（SRS FR-05）。"""

    inner: Any
    k: int

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return list(self.inner.invoke(query))[: self.k]

    async def _aget_relevant_documents(
        self, query: str, *, run_manager
    ) -> list[Document]:
        return list(await self.inner.ainvoke(query))[: self.k]


def _project_filter(project_id: int):
    """Qdrant 过滤器：project_id 强制隔离（多租户，SRS FR-05）。

    局部 import qdrant SDK：LangChain 检索路径本身已绑定 langchain_qdrant；
    D2「业务层禁止 import SDK」约束针对资产管道层（VectorStore Protocol 消费方）。
    """
    from qdrant_client.http import models as qmodels

    return qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="project_id", match=qmodels.MatchValue(value=project_id)
            )
        ]
    )


async def _load_bm25_documents(project_id: int, limit: int = 1000) -> list[Document]:
    """滚动拉取项目内全部 chunk 作 BM25 语料（payload → Document）。

    失败仅告警返回空列表（BM25 为增强项，降级纯向量检索，NFR-02）。
    """
    try:
        from qdrant_client.http import models as qmodels

        base = get_vector_store()
        points, _ = await base._client.scroll(
            collection_name=base._collection,
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="project_id", match=qmodels.MatchValue(value=project_id)
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
        )
        docs: list[Document] = []
        for p in points or []:
            payload = p.payload or {}
            text = payload.get("text_chunk", "")
            if not text:
                continue
            metadata = {k: v for k, v in payload.items() if k != "text_chunk"}
            docs.append(Document(page_content=text, metadata=metadata))
        return docs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"BM25 语料加载失败（降级为纯向量检索）: {exc}")
        return []


async def build_retriever(
    project_id: int,
    top_k: int | None = None,
    threshold: float | None = None,
    use_bm25: bool | None = None,
) -> BaseRetriever:
    """FR-05：Top-k + 相似度阈值 + project_id 过滤；use_bm25 时叠加 BM25 混检。

    - 向量检索：similarity_score_threshold（PTPQdrant 已把 Qdrant COSINE 相似度
      映射为恒等 relevance，score_threshold 即"相似度 ≥ 阈值"）
    - BM25 混检：EnsembleRetriever（权重 0.5/0.5）合并去重，_TopKRetriever 截断；
      语料为空或加载失败时降级纯向量检索
    """
    settings = get_settings()
    if top_k is None:
        top_k = settings.top_k
    if threshold is None:
        threshold = settings.similarity_threshold
    if use_bm25 is None:
        use_bm25 = settings.use_bm25

    store = get_langchain_vector_store()
    retriever = store.as_retriever(
        search_type="similarity_score_threshold",
        search_kwargs={
            "k": top_k,
            "score_threshold": threshold,
            "filter": _project_filter(project_id),
        },
    )

    if use_bm25:
        from langchain_classic.retrievers import EnsembleRetriever
        from langchain_community.retrievers import BM25Retriever

        corpus = await _load_bm25_documents(project_id)
        if corpus:
            bm25 = BM25Retriever.from_documents(corpus)
            bm25.k = top_k
            ensemble = EnsembleRetriever(
                retrievers=[bm25, retriever], weights=[0.5, 0.5]
            )
            retriever = _TopKRetriever(inner=ensemble, k=top_k)
        else:
            logger.warning("BM25 语料为空，降级为纯向量检索")

    return retriever


# ---------- FR-06 Promptor ----------


def build_context(docs: list[Document], max_chars: int | None = None) -> str:
    """FR-06 证据拼接：每块前置引用标识，超长按块截断至 max_context_chars（≤1800）。"""
    settings = get_settings()
    limit = settings.max_context_chars if max_chars is None else max_chars
    blocks: list[str] = []
    total = 0
    truncated = False
    for doc in docs:
        block = f"[{format_citation(doc)}] {doc.page_content}"
        if total + len(block) > limit:
            remaining = limit - total
            if remaining > 0:
                blocks.append(block[:remaining])
            truncated = True
            break
        blocks.append(block)
        total += len(block) + 2  # "\n\n" join 开销
    if truncated:
        logger.debug(f"context 超长，已截断至 {limit} 字符")
    return "\n\n".join(blocks)


def build_prompt() -> ChatPromptTemplate:
    """FR-06：系统约束 + 问题改写 + 证据拼接 + 引用占位符 + 可选历史/scratchpad。"""
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder("chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad", optional=True),
        ]
    )


# ---------- FR-07 LLM 编排 ----------


def _supports_tool_calling(llm) -> bool:
    """判定模型是否支持 Function Calling。

    FakeListChatModel.bind_tools 抛 NotImplementedError（Mock 路径不走工具编排，
    实施方案风险表对策）；ChatOpenAI 等返回 RunnableBinding。
    """
    try:
        llm.bind_tools([])
    except NotImplementedError:
        return False
    return True


async def _run_agent_path(
    llm,
    system_text: str,
    question: str,
    history: list,
    docs: list[Document],
    retriever,
) -> AnswerOutput:
    """Function Calling 路径（langgraph create_react_agent）；失败由调用方降级。

    result.messages 含 ToolMessage（工具调用链可追踪，SRS FR-07 验收口径）。
    """
    from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(llm, build_tools(retriever), prompt=system_text)
    result = await agent.ainvoke({"messages": [*history, ("user", question)]})
    messages = result.get("messages") or []
    raw = messages[-1].content if messages else ""
    return parse_answer(raw, docs)


async def _retrieve_context(
    project_id: int,
    question: str,
    top_k: int | None,
    threshold: float | None,
) -> tuple[BaseRetriever, list[Document], str, str]:
    """FR-05/06 共享准备：检索 → context 拼接 → 引用标识文本。

    返回 (retriever, docs, context, citations_text)；检索异常直接外抛，
    由同步/流式入口按各自降级策略处理（503 / error 事件）。
    """
    retriever = await build_retriever(project_id, top_k=top_k, threshold=threshold)
    docs: RetrieverOutput = await retriever.ainvoke(question)
    context = build_context(docs)
    citations = [format_citation(d) for d in docs]
    citations_text = "、".join(citations) if citations else "（无）"
    return retriever, docs, context, citations_text


async def _run_qa_chain_impl(
    project_id: int,
    question: str,
    chat_history: list | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    use_tools: bool = True,
) -> AnswerOutput:
    """FR-05/06/07 编排内核：检索 → context → LLM（工具编排或纯 RAG）→ 解析/降级。

    返回 FR-09 统一 AnswerOutput，任何异常均不外抛（降级兜底，SRS 1.5.4）。
    """
    retriever, docs, context, citations_text = await _retrieve_context(
        project_id, question, top_k, threshold
    )
    history = list(chat_history or [])

    llm = get_llm()
    system_text = SYSTEM_PROMPT.format(
        context=context or "（空）", citations=citations_text
    )

    if use_tools and _supports_tool_calling(llm):
        try:
            return await _run_agent_path(
                llm, system_text, question, history, docs, retriever
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"工具编排路径失败，降级: {type(exc).__name__}: {exc}")
            return _fallback_payload(docs, f"工具编排路径失败: {type(exc).__name__}")

    chain = build_prompt() | llm | StrOutputParser()
    try:
        raw = await chain.ainvoke(
            {
                "input": question,
                "context": context,
                "citations": citations_text,
                "chat_history": history,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"LLM 调用失败，降级: {type(exc).__name__}: {exc}")
        return _fallback_payload(docs, f"LLM 调用失败: {type(exc).__name__}")
    return parse_answer(raw, docs)


async def run_qa_chain(
    project_id: int,
    question: str,
    chat_history: list | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    use_tools: bool = True,
    run_no: str | int | None = None,
) -> AnswerOutput:
    """FR-05~08 编排入口：先跑 FR-05/06/07 问答内核，run_no 非空时接 FR-08 指标校验。

    run_no 携带（问题明确指向某次运行）时，对 AnswerOutput 做 ±5% 容差校验，
    超差压 confidence、notes 写 mismatch（SRS FR-08）；校验自身异常不外溢，
    返回未校验的原始结果（NFR-02：guardrail 不影响主问答链路可用性）。
    """
    result = await _run_qa_chain_impl(
        project_id,
        question,
        chat_history=chat_history,
        top_k=top_k,
        threshold=threshold,
        use_tools=use_tools,
    )
    if run_no is None:
        return result
    try:
        return await validate_answer(run_no, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"FR-08 指标校验执行异常，返回未校验结果: run={run_no} "
            f"{type(exc).__name__}: {exc}"
        )
        return result


# ---------- FR-10 流式编排（SSE /chat/stream 与 WS /ws/chat 共用） ----------


def _chunk_to_text(chunk: Any) -> str:
    """从流式 chunk 取文本：content 为 str 直接用；为 block 列表时拼接文本块。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content or "")


async def search_knowledge(
    project_id: int,
    query: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[tuple[Document, float]]:
    """FR-05 带相似度分的向量检索（GET /assets/knowledge-search、TC-RET-001）。

    走 LangChain `asimilarity_search_with_relevance_scores`（PTPQdrant 恒等
    relevance 映射，分数即 COSINE 相似度 0~1），手动按 threshold 过滤后截断
    top_k；BM25 混检是对话链内的增强能力，本接口只呈现向量相似度分。
    """
    settings = get_settings()
    if top_k is None:
        top_k = settings.top_k
    if threshold is None:
        threshold = settings.similarity_threshold
    store = get_langchain_vector_store()
    pairs = await store.asimilarity_search_with_relevance_scores(
        query, k=top_k, filter=_project_filter(project_id)
    )
    return [(doc, score) for doc, score in pairs if score >= threshold][:top_k]


async def astream_qa_events(
    project_id: int,
    question: str,
    chat_history: list | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    use_tools: bool = True,
    run_no: str | int | None = None,
) -> AsyncIterator[dict]:
    """FR-10 流式事件生成器（SSE /chat/stream 与 WS /ws/chat 共用）。

    事件协议（SRS 6.2）：
    - {"type": "token", "content": "..."}      LLM 增量文本（至少 1 个）
    - {"type": "tool_call", "tool", "args"}    工具调用进度（仅真实 LLM
      agent 路径产出；Mock 路径不触发 tool_calls）
    - {"type": "error", "message": "..."}      可恢复错误提示（后接 done）
    - {"type": "done", "final": AnswerOutput}  终态（FR-09 五字段，必达）

    检索失败/LLM 失败均不外抛：发 error 事件后以兜底 AnswerOutput 收尾
    （SRS 1.5.4 + NFR-02）；run_no 非空时 done 前接 FR-08 指标校验。
    """
    try:
        retriever, docs, context, citations_text = await _retrieve_context(
            project_id, question, top_k, threshold
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"流式问答检索失败，降级收尾: {type(exc).__name__}: {exc}")
        fallback = _fallback_payload([], f"知识检索失败: {type(exc).__name__}")
        yield {"type": "error", "message": "知识检索失败，已返回降级响应"}
        yield {"type": "done", "final": fallback.model_dump()}
        return

    history = list(chat_history or [])
    llm = get_llm()
    system_text = SYSTEM_PROMPT.format(
        context=context or "（空）", citations=citations_text
    )

    answer: AnswerOutput
    if use_tools and _supports_tool_calling(llm):
        # 真实 LLM 的 agent 编排无逐 token 流：整段回答作为单个 token 事件。
        # 工具调用进度（tool_call 事件）的流式钩子留待真模型联调时补
        # （langgraph astream_events），Mock 路径不进入本分支。
        try:
            answer = await _run_agent_path(
                llm, system_text, question, history, docs, retriever
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"流式工具编排失败，降级: {type(exc).__name__}: {exc}")
            answer = _fallback_payload(docs, f"工具编排路径失败: {type(exc).__name__}")
        yield {"type": "token", "content": answer.answer}
    else:
        chain = build_prompt() | llm
        raw_parts: list[str] = []
        try:
            async for chunk in chain.astream(
                {
                    "input": question,
                    "context": context,
                    "citations": citations_text,
                    "chat_history": history,
                }
            ):
                piece = _chunk_to_text(chunk)
                if not piece:
                    continue
                raw_parts.append(piece)
                yield {"type": "token", "content": piece}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"流式 LLM 调用失败，降级: {type(exc).__name__}: {exc}")
            answer = _fallback_payload(docs, f"LLM 调用失败: {type(exc).__name__}")
            yield {"type": "error", "message": "LLM 调用失败，已返回降级响应"}
        else:
            answer = parse_answer("".join(raw_parts), docs)

    if run_no is not None:
        try:
            answer = await validate_answer(run_no, answer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"流式 FR-08 校验异常，返回未校验结果: run={run_no} "
                f"{type(exc).__name__}: {exc}"
            )

    yield {"type": "done", "final": answer.model_dump()}
