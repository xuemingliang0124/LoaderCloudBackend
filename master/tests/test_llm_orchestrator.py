"""P3 Stage 2 orchestrator/client 测试（FR-05/06/07/09）。

覆盖验收用例：TC-PRM-001（模板与上下文拼接）、TC-LLM-001（正常调用解析）、
TC-LLM-002（解析降级）、TC-OUT-001（输出字段完整）。

Mock 约束（实施方案风险表）：FakeListChatModel 不触发 tool_calls（bind_tools 抛
NotImplementedError），Mock 路径只走纯 RAG 链 + fallback；Function Calling 路径
需真实 LLM（Stage 5 评测覆盖）。

打桩范式（沿 test_asset_parse_pipeline）：monkeypatch orchestrator/client 命名
空间内 get_settings / get_llm / build_retriever / get_langchain_vector_store。
"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever
from langchain_openai import ChatOpenAI

import app.services.llm.client as client_mod
import app.services.llm.orchestrator as orch
from app.services.llm.client import (
    FALLBACK_RESPONSE,
    AnswerOutput,
    _fallback_payload,
    format_citation,
    normalize_json,
    parse_answer,
    reset_llm,
)
from app.services.vector_store import reset_langchain_vector_store


def _stub_settings(**overrides) -> SimpleNamespace:
    base = dict(
        top_k=5,
        similarity_threshold=0.5,
        use_bm25=False,
        max_context_chars=1800,
        llm_provider="",
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_base_url_resolved="",
        llm_temperature=0.1,
        llm_timeout=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _doc(
    i: int, asset_id: int = 12, asset_type: str = "plan_doc", size: int = 50
) -> Document:
    return Document(
        page_content=f"chunk-{i} " + "字" * size,
        metadata={
            "asset_id": asset_id,
            "asset_type": asset_type,
            "chunk_index": i,
            "project_id": 1,
        },
    )


class FakeRetriever(BaseRetriever):
    docs: list[Document] = []

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return list(self.docs)


class FakeStore:
    """捕获 as_retriever 参数的假向量库（验证 search_kwargs 组装）。"""

    def __init__(self) -> None:
        self.captured: dict | None = None

    def as_retriever(self, **kwargs):
        self.captured = kwargs
        return FakeRetriever(docs=[])


@pytest.fixture(autouse=True)
def _reset_llm_singletons():
    reset_llm()
    reset_langchain_vector_store()
    yield
    reset_llm()
    reset_langchain_vector_store()


@pytest.fixture
def stub_settings(monkeypatch):
    s = _stub_settings()
    monkeypatch.setattr(orch, "get_settings", lambda: s)
    return s


def _patch_retriever(monkeypatch, docs: list[Document]) -> None:
    async def fake_build_retriever(project_id, **kwargs):
        return FakeRetriever(docs=docs)

    monkeypatch.setattr(orch, "build_retriever", fake_build_retriever)


# ---- client.format_citation（SRS 5.3 引用标识）----


def test_format_citation_from_metadata() -> None:
    doc = Document(
        page_content="x",
        metadata={"asset_id": 12, "asset_type": "plan_doc", "chunk_index": 3},
    )
    assert format_citation(doc) == "plan_doc:12:chunk_3"


def test_format_citation_missing_metadata_falls_back_placeholders() -> None:
    doc = Document(page_content="x", metadata={})
    assert format_citation(doc) == "unknown:unknown:chunk_0"


# ---- client.get_llm（FR-07 + NFR-01 降级）----


def test_get_llm_fake_when_unconfigured(monkeypatch) -> None:
    """未配置 LLM_API_KEY/MODEL/BASE_URL → FakeListChatModel 降级（不抛异常）。"""
    monkeypatch.setattr(client_mod, "get_settings", lambda: _stub_settings())
    llm = client_mod.get_llm()
    assert isinstance(llm, FakeListChatModel)
    # 预设响应是合法 AnswerOutput JSON，且 citations 为空（由 Top-1 兜底）
    parsed = parse_answer(FALLBACK_RESPONSE, [_doc(0)])
    assert isinstance(parsed, AnswerOutput)
    assert parsed.citations == ["plan_doc:12:chunk_0"]


def test_get_llm_chatopenai_when_configured(monkeypatch) -> None:
    """已配置 → ChatOpenAI（OpenAI 兼容协议，智谱/阿里）。"""
    monkeypatch.setattr(
        client_mod,
        "get_settings",
        lambda: _stub_settings(
            llm_api_key="key-1",
            llm_model="glm-4-flash",
            llm_base_url_resolved="https://open.bigmodel.cn/api/paas/v4",
        ),
    )
    llm = client_mod.get_llm()
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "glm-4-flash"


def test_get_llm_singleton_reset(monkeypatch) -> None:
    """单例：两次调用同实例；reset 后为新实例（测试夹具可重置）。"""
    monkeypatch.setattr(client_mod, "get_settings", lambda: _stub_settings())
    llm1 = client_mod.get_llm()
    assert client_mod.get_llm() is llm1
    reset_llm()
    assert client_mod.get_llm() is not llm1


# ---- client.parse_answer / fallback（FR-07 异常处理 / FR-09 字段补齐）----


def test_normalize_json_extracts_braced_substring() -> None:
    assert normalize_json('回答如下：{"answer":"a"} 完毕') == '{"answer":"a"}'
    assert normalize_json('{"answer":"a"}') == '{"answer":"a"}'
    assert normalize_json("no braces") == "no braces"
    assert normalize_json("") == ""


def test_parse_answer_valid_json() -> None:
    raw = json.dumps(
        {
            "answer": "TPS 480",
            "citations": ["run_summary:1001"],
            "used_metrics": ["tps"],
            "confidence": 0.9,
            "notes": "",
        },
        ensure_ascii=False,
    )
    out = parse_answer(raw, [_doc(0)])
    assert out.answer == "TPS 480"
    assert out.citations == ["run_summary:1001"]
    assert out.used_metrics == ["tps"]
    assert out.confidence == 0.9
    assert out.notes == ""


def test_parse_answer_fenced_markdown_json() -> None:
    """markdown 围栏由 PydanticOutputParser 自行剥离（探针验证）。"""
    raw = '```json\n{"answer":"a","citations":["x:1:chunk_0"],"confidence":0.8}\n```'
    out = parse_answer(raw, [_doc(0)])
    assert out.answer == "a"
    assert out.citations == ["x:1:chunk_0"]


def test_parse_answer_prefix_junk_normalized() -> None:
    """前后缀垃圾文本由 normalize_json 抽取 {} 子串修复。"""
    raw = '回答：{"answer":"a","citations":["x:1:chunk_0"]} 以上。'
    out = parse_answer(raw, [])
    assert out.answer == "a"
    assert out.citations == ["x:1:chunk_0"]


def test_parse_answer_garbage_falls_back_with_top1_citation() -> None:
    """TC-LLM-002：非法 JSON → 兜底，citations 取检索 Top-1，notes 标注降级。"""
    out = parse_answer("这不是JSON", [_doc(0), _doc(1)])
    assert isinstance(out, AnswerOutput)
    assert out.citations == ["plan_doc:12:chunk_0"]
    assert "解析失败" in out.notes
    assert out.confidence == 0.5
    assert out.answer


def test_parse_answer_wrong_types_falls_back() -> None:
    """字段类型错误（Pydantic 校验失败）同样走兜底。"""
    out = parse_answer('{"answer": 123, "citations": "bad"}', [_doc(2)])
    assert out.citations == ["plan_doc:12:chunk_2"]
    assert "解析失败" in out.notes


def test_parse_answer_empty_citations_backfills_top1() -> None:
    """SRS FR-07：引用为空 → 兜底填充检索 Top-1 的引用。"""
    raw = json.dumps(
        {"answer": "a", "citations": [], "confidence": 0.7}, ensure_ascii=False
    )
    out = parse_answer(raw, [_doc(1)])
    assert out.citations == ["plan_doc:12:chunk_1"]
    assert "Top-1 兜底" in out.notes


def test_parse_answer_empty_citations_no_docs_stays_empty() -> None:
    """空库检索无结果时 citations 保持为空（无来源可引，不编造）。"""
    raw = json.dumps({"answer": "a", "citations": []}, ensure_ascii=False)
    out = parse_answer(raw, [])
    assert out.citations == []


def test_fallback_payload_empty_docs() -> None:
    """空库兜底：五字段完整、citations 为空、answer 给出明确降级说明。"""
    out = _fallback_payload([], "LLM 超时")
    assert isinstance(out, AnswerOutput)
    assert out.citations == []
    assert out.notes == "LLM 超时"
    assert out.confidence == 0.5
    assert "降级" in out.answer


# ---- orchestrator.build_context（FR-06 证据拼接，TC-PRM-001）----


def test_build_context_truncates_to_max_chars(stub_settings) -> None:
    docs = [_doc(i, size=900) for i in range(3)]
    context = orch.build_context(docs)
    assert len(context) <= stub_settings.max_context_chars


def test_build_context_prefixes_citation_marker(stub_settings) -> None:
    context = orch.build_context([_doc(0)])
    assert context.startswith("[plan_doc:12:chunk_0] ")


def test_build_context_empty_docs_returns_empty(stub_settings) -> None:
    assert orch.build_context([]) == ""


# ---- orchestrator.build_prompt（FR-06，TC-PRM-001）----


def test_build_prompt_renders_system_with_context_and_citations() -> None:
    prompt = orch.build_prompt()
    messages = prompt.invoke(
        {
            "input": "登录交易压到 500 TPS",
            "context": "上下文内容",
            "citations": "plan_doc:12:chunk_3",
        }
    ).to_messages()
    assert len(messages) == 2  # 无历史：system + human
    system_text = messages[0].content
    assert "上下文内容" in system_text
    assert "plan_doc:12:chunk_3" in system_text
    assert "禁止编造" in system_text
    assert "[plan_doc:12:chunk_3]" in system_text  # 引用格式示例
    assert messages[1].content == "登录交易压到 500 TPS"


def test_build_prompt_chat_history_optional() -> None:
    prompt = orch.build_prompt()
    messages = prompt.invoke(
        {
            "input": "q",
            "context": "c",
            "citations": "x",
            "chat_history": [("human", "hi"), ("ai", "hello")],
        }
    ).to_messages()
    assert len(messages) == 4  # system + 2 历史 + human


# ---- orchestrator.build_retriever（FR-05：过滤/参数/混检）----


async def test_build_retriever_passes_project_filter_and_kwargs(
    stub_settings, monkeypatch
) -> None:
    """search_type/score_threshold/k/filter 组装正确，project_id 强制隔离。"""
    store = FakeStore()
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: store)
    await orch.build_retriever(1001)
    assert store.captured is not None
    assert store.captured["search_type"] == "similarity_score_threshold"
    kwargs = store.captured["search_kwargs"]
    assert kwargs["k"] == 5
    assert kwargs["score_threshold"] == 0.5
    flt = kwargs["filter"]
    keys = {c.key for c in flt.must}
    values = {c.key: c.match.value for c in flt.must}
    assert keys == {"project_id"}
    assert values["project_id"] == 1001


async def test_build_retriever_respects_explicit_overrides(
    stub_settings, monkeypatch
) -> None:
    store = FakeStore()
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: store)
    await orch.build_retriever(1, top_k=3, threshold=0.7)
    kwargs = store.captured["search_kwargs"]
    assert kwargs["k"] == 3
    assert kwargs["score_threshold"] == 0.7


async def test_build_retriever_bm25_ensemble_dedup_and_topk(
    stub_settings, monkeypatch
) -> None:
    """use_bm25 → EnsembleRetriever 混检：合并去重 + 截断 ≤ top_k（SRS FR-05）。"""
    stub_settings.use_bm25 = True
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: FakeStore())

    corpus = [
        Document(
            page_content="登录交易 500TPS 压测",
            metadata={"id": "d0", "asset_id": 1, "chunk_index": 0},
        ),
        Document(
            page_content="其他文档内容",
            metadata={"id": "d1", "asset_id": 1, "chunk_index": 1},
        ),
    ]
    monkeypatch.setattr(orch, "_load_bm25_documents", _async_corpus(corpus))

    retriever = await orch.build_retriever(1)
    # EnsembleRetriever 被 _TopKRetriever 包装
    assert isinstance(retriever.inner, _ensemble_cls())
    docs = await retriever.ainvoke("登录")
    ids = [d.metadata.get("id") for d in docs]
    assert len(ids) == len(set(ids))  # 去重
    assert len(docs) <= 5  # ≤ top_k


def _ensemble_cls():
    from langchain_classic.retrievers import EnsembleRetriever

    return EnsembleRetriever


def _async_corpus(value):
    """打桩 _load_bm25_documents：返回 list[Document]（与真实签名一致）。"""

    async def _inner(project_id, limit=1000):
        return value

    return _inner


async def test_build_retriever_bm25_empty_corpus_degrades(
    stub_settings, monkeypatch
) -> None:
    """BM25 语料为空 → 降级纯向量检索（不抛异常）。"""
    stub_settings.use_bm25 = True
    store = FakeStore()
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: store)
    monkeypatch.setattr(orch, "_load_bm25_documents", _async_corpus([]))
    retriever = await orch.build_retriever(1)
    assert not isinstance(retriever, orch._TopKRetriever)


# ---- orchestrator.run_qa_chain（FR-05/06/07 编排，TC-LLM-001/002 + TC-OUT-001）----


async def test_run_qa_chain_parses_valid_json(stub_settings, monkeypatch) -> None:
    """TC-LLM-001 + TC-OUT-001：正常调用返回五字段完整 AnswerOutput。"""
    payload = {
        "answer": "本次压测 TPS 为 480，P95 为 120ms [plan_doc:12:chunk_0]",
        "citations": ["plan_doc:12:chunk_0"],
        "used_metrics": ["tps", "p95_ms"],
        "confidence": 0.9,
        "notes": "",
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0), _doc(1)])

    result = await orch.run_qa_chain(1, "登录交易压测结果如何")
    assert isinstance(result, AnswerOutput)
    assert result.answer == payload["answer"]
    assert result.citations == ["plan_doc:12:chunk_0"]
    assert result.used_metrics == ["tps", "p95_ms"]
    assert result.confidence == 0.9
    assert isinstance(result.notes, str)


async def test_run_qa_chain_garbage_falls_back(stub_settings, monkeypatch) -> None:
    """TC-LLM-002：LLM 返回垃圾 → 兜底 payload，citations 取 Top-1 非空。"""
    monkeypatch.setattr(
        orch, "get_llm", lambda: FakeListChatModel(responses=["这不是JSON"])
    )
    _patch_retriever(monkeypatch, [_doc(0), _doc(1)])

    result = await orch.run_qa_chain(1, "q")
    assert isinstance(result, AnswerOutput)
    assert result.citations == ["plan_doc:12:chunk_0"]
    assert "解析失败" in result.notes
    assert result.confidence == 0.5


async def test_run_qa_chain_empty_retrieval_no_crash(
    stub_settings, monkeypatch
) -> None:
    """空库检索返回空列表不报错，citations 为空（NFR-02 鲁棒性）。"""
    payload = {"answer": "知识库为空", "citations": [], "confidence": 0.5, "notes": ""}
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [])

    result = await orch.run_qa_chain(1, "q")
    assert isinstance(result, AnswerOutput)
    assert result.citations == []
    assert result.answer == "知识库为空"


async def test_run_qa_chain_llm_failure_falls_back(stub_settings, monkeypatch) -> None:
    """LLM 调用异常（超时/网络）→ 兜底 payload，不外抛（SRS 1.5.4）。"""

    class BoomLLM(FakeListChatModel):
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(orch, "get_llm", lambda: BoomLLM(responses=["x"]))
    _patch_retriever(monkeypatch, [_doc(0)])

    result = await orch.run_qa_chain(1, "q")
    assert isinstance(result, AnswerOutput)
    assert "LLM 调用失败" in result.notes
    assert result.citations == ["plan_doc:12:chunk_0"]


async def test_run_qa_chain_mock_skips_agent_path(stub_settings, monkeypatch) -> None:
    """FakeListChatModel（bind_tools 抛 NotImplementedError）不走工具编排路径。"""
    called = {"agent": False}

    class TrackingFake(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):
            called["agent"] = True
            raise NotImplementedError

    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: TrackingFake(
            responses=[json.dumps({"answer": "a", "citations": []}, ensure_ascii=False)]
        ),
    )
    _patch_retriever(monkeypatch, [_doc(0)])
    await orch.run_qa_chain(1, "q", use_tools=True)
    assert called["agent"] is True  # 检测触发但未进入 agent（回退纯 RAG 链）


def test_supports_tool_calling_detection() -> None:
    """ChatOpenAI 支持 bind_tools；FakeListChatModel 抛 NotImplementedError。"""
    from langchain_openai import ChatOpenAI

    assert orch._supports_tool_calling(ChatOpenAI(model="g", api_key="k")) is True
    assert orch._supports_tool_calling(FakeListChatModel(responses=["x"])) is False


# ---- Stage 4：astream_qa_events / search_knowledge（FR-10）----


async def test_astream_qa_events_emits_token_then_done(
    stub_settings, monkeypatch
) -> None:
    """流式：token 事件（≥1）→ done，done.final 为解析后的五字段。"""
    payload = {
        "answer": "TPS 480",
        "citations": ["plan_doc:12:chunk_0"],
        "confidence": 0.8,
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0)])

    events = [e async for e in orch.astream_qa_events(1, "q")]
    assert events[0]["type"] == "token"
    assert "".join(e["content"] for e in events if e["type"] == "token")
    assert events[-1]["type"] == "done"
    final = events[-1]["final"]
    assert final["answer"] == "TPS 480"
    assert final["citations"] == ["plan_doc:12:chunk_0"]
    assert not [e for e in events if e["type"] == "error"]


async def test_astream_qa_events_retrieval_failure_degrades_to_error_done(
    stub_settings, monkeypatch
) -> None:
    """检索基础设施异常不外抛：error 事件后必接 done（兜底 AnswerOutput）。"""

    async def _boom(*args, **kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(orch, "build_retriever", _boom)
    events = [e async for e in orch.astream_qa_events(1, "q")]
    assert events[0]["type"] == "error"
    assert events[-1]["type"] == "done"
    assert events[-1]["final"]["citations"] == []
    assert events[-1]["final"]["answer"]


async def test_search_knowledge_returns_scored_pairs(
    stub_settings, monkeypatch
) -> None:
    """带分检索：返回 (Document, score) 且按阈值 0.5 过滤 + top_k 截断。"""

    class ScoredStore:
        async def asimilarity_search_with_relevance_scores(self, query, k=4, **kwargs):
            return [(_doc(0), 0.91), (_doc(1), 0.62), (_doc(2), 0.30)]

    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: ScoredStore())
    pairs = await orch.search_knowledge(1, "登录 TPS")
    assert [round(s, 2) for _, s in pairs] == [0.91, 0.62]
    assert pairs[0][0].page_content.startswith("chunk-0")
