"""P3 Stage 4 LLM 对话 REST/SSE/知识检索入口测试（FR-10，SRS 第 6 章）。

覆盖验收点：
- TC-API-001：POST /chat 同步问答五字段（code/message/data 统一包裹）
- TC-API-002：POST /chat/stream SSE 事件流（token → done，done.final 五字段）
- GET /assets/knowledge-search：带分命中（TC-RET-001）+ 4003 向量库降级
- 401 未登录 / 422 参数非法 / 3030 非成员门禁 / run_no 不存在 4004
- LLM 不可用 → 503 + 错误码 4000 + 降级 JSON

打桩：monkeypatch orchestrator 命名空间内 get_settings/get_llm/
build_retriever/get_langchain_vector_store（沿 test_llm_orchestrator 范式）；
SSE 端点的前置门禁走独立 SessionLocal，打桩为 db_env。
"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever

import app.api.v1.chat as chat_api
import app.services.llm.orchestrator as orch
from app.core.security import create_access_token
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services.llm.client import reset_llm
from app.services.vector_store import reset_langchain_vector_store


def _auth(username: str, role: str = "viewer") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


def _stub_settings() -> SimpleNamespace:
    return SimpleNamespace(
        top_k=5,
        similarity_threshold=0.5,
        use_bm25=False,
        max_context_chars=1800,
    )


def _doc(i: int) -> Document:
    return Document(
        page_content=f"登录交易 TPS 480，P95 120ms 片段{i}",
        metadata={
            "asset_id": 12,
            "asset_type": "plan_doc",
            "chunk_index": i,
            "project_id": 1,
        },
    )


class FakeRetriever(BaseRetriever):
    docs: list[Document] = []

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return list(self.docs)


def _valid_answer_json() -> str:
    return json.dumps(
        {
            "answer": "登录交易基准 TPS 为 480，P95 为 120ms [plan_doc:12:chunk_0]",
            "citations": ["plan_doc:12:chunk_0"],
            "used_metrics": ["tps", "p95_ms"],
            "confidence": 0.9,
            "notes": "",
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_llm()
    reset_langchain_vector_store()
    yield
    reset_llm()
    reset_langchain_vector_store()


@pytest.fixture
def _llm_stub(monkeypatch):
    """orchestrator 层 Mock：假设置 + FakeListChatModel + 假检索器。"""
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    monkeypatch.setattr(
        orch, "get_llm", lambda: FakeListChatModel(responses=[_valid_answer_json()])
    )

    async def _build(project_id, **kwargs):
        return FakeRetriever(docs=[_doc(0), _doc(1)])

    monkeypatch.setattr(orch, "build_retriever", _build)


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username, "viewer")
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _add_member(db_session, project_id: int, username: str, role: str) -> None:
    db_session.add(
        ProjectMember(
            project_id=project_id, username=username, role=role, granted_by="system"
        )
    )
    await db_session.commit()


async def _seed_run(db_session, project_id: int, run_no: str) -> None:
    scenario = Scenario(project_id=project_id, name=f"场景-{run_no}")
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(ScenarioRun(run_no=run_no, scenario_id=scenario.id))
    await db_session.commit()


# ---------- POST /chat ----------


async def test_chat_requires_auth(client) -> None:
    """无 Authorization → 401（不触库/不触 LLM）。"""
    r = await client.post("/api/v1/chat", json={"project_id": 1, "message": "hi"})
    assert r.status_code == 401


async def test_chat_validation_422(client) -> None:
    """必填缺失/超长/空串 → 422（SRS FR-10）。"""
    r = await client.post("/api/v1/chat", json={"project_id": 1}, headers=_auth("a"))
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": 1, "message": "x" * 2001},
        headers=_auth("a"),
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": 0, "message": "hi"},
        headers=_auth("a"),
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": 1, "message": "top_k 越界", "top_k": 21},
        headers=_auth("a"),
    )
    assert r.status_code == 422


async def test_chat_non_member_rejected(client, _llm_stub) -> None:
    """非项目成员 → BusinessError 3030（项目门禁先于 LLM 调用）。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "登录交易 TPS 如何"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_chat_success_returns_answer_out(client, _llm_stub) -> None:
    """TC-API-001：成员问答 → code=0，data 为 FR-09 五字段完整结构。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "登录交易压测结果如何", "top_k": 5},
        headers=_auth("alice"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data.keys()) == {
        "answer",
        "citations",
        "used_metrics",
        "confidence",
        "notes",
    }
    assert "480" in data["answer"]
    assert data["citations"] == ["plan_doc:12:chunk_0"]
    assert data["used_metrics"] == ["tps", "p95_ms"]
    assert data["confidence"] == 0.9
    assert isinstance(data["notes"], str)


async def test_chat_run_no_missing_returns_4004(client, db_session, _llm_stub) -> None:
    """携带不存在 run_no → 4004（SRS 6.3 指标校验失败），且不进入 LLM。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q", "run_no": "R-MISSING"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 4004


async def test_chat_run_no_visible_passes(
    client, db_session, monkeypatch, _llm_stub
) -> None:
    """run_no 存在且可见 → 放行（FR-08 校验在 guardrail，ES 异常已由编排层吞掉）。"""
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260920001")
    # guardrail 取基准指标依赖 ES：打桩为无基准（notes 标注，不阻断）
    import app.services.llm.guardrail as guard

    async def _no_metrics(run_no):
        return None

    monkeypatch.setattr(guard.es_client, "get_run_metrics", _no_metrics)

    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q", "run_no": "R20260920001"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["answer"]


async def test_chat_llm_unavailable_returns_503_degraded(client, monkeypatch) -> None:
    """链路异常 → 503 + code 4000 + data 降级 AnswerOut（SRS FR-10）。"""
    pid = await _create_project(client, "项目A")

    async def _boom(*args, **kwargs):
        raise RuntimeError("llm backend down")

    monkeypatch.setattr(chat_api, "run_qa_chain", _boom)
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == 4000
    assert body["data"]["answer"]
    assert body["data"]["citations"] == []
    assert "LLM 服务不可用" in body["message"]


# ---------- POST /chat/stream（SSE） ----------


async def test_chat_stream_sse_token_then_done(
    client, db_env, monkeypatch, _llm_stub
) -> None:
    """TC-API-002：SSE 头 + data: 帧序列 token* → done（final 五字段完整）。"""
    # SSE 端点前置门禁用独立 SessionLocal，指向测试内存库 maker
    monkeypatch.setattr(chat_api, "SessionLocal", db_env)
    pid = await _create_project(client, "项目A")

    async with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"project_id": pid, "message": "登录交易压测结果如何"},
        headers=_auth("alice"),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert types[0] == "token"
    assert types[-1] == "done"
    # token 拼接包含答案内容
    assert "480" in "".join(
        e.get("content", "") for e in events if e["type"] == "token"
    )
    final = events[-1]["final"]
    assert set(final.keys()) == {
        "answer",
        "citations",
        "used_metrics",
        "confidence",
        "notes",
    }
    assert final["citations"] == ["plan_doc:12:chunk_0"]


async def test_chat_stream_requires_auth(client) -> None:
    r = await client.post("/api/v1/chat/stream", json={"project_id": 1, "message": "x"})
    assert r.status_code == 401


async def test_chat_stream_non_member_blocked(
    client, db_env, monkeypatch, _llm_stub
) -> None:
    """门禁在流式握手前完成：非成员直接 400/3030，拿不到事件流。"""
    monkeypatch.setattr(chat_api, "SessionLocal", db_env)
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat/stream",
        json={"project_id": pid, "message": "q"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


# ---------- GET /assets/knowledge-search ----------


class _FakeScoredStore:
    """带相似度分的假 LangChain 向量库。"""

    def __init__(self, pairs: list[tuple[Document, float]]) -> None:
        self._pairs = pairs
        self.calls: list[dict] = []

    async def asimilarity_search_with_relevance_scores(self, query, k=4, **kwargs):
        self.calls.append({"query": query, "k": k, **kwargs})
        return self._pairs


async def test_knowledge_search_returns_scored_items(client, monkeypatch) -> None:
    """TC-RET-001：返回 ≤top_k 命中切片，每项含 score/引用/元数据，按阈值过滤。"""
    pid = await _create_project(client, "项目A")
    pairs = [(_doc(0), 0.91), (_doc(1), 0.62)]
    fake_store = _FakeScoredStore(pairs)
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: fake_store)

    r = await client.get(
        "/api/v1/assets/knowledge-search",
        params={"project_id": pid, "q": "登录交易 TPS", "top_k": 5},
        headers=_auth("alice"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == 2
    first = data["items"][0]
    assert first["score"] == 0.91
    assert first["citation"] == "plan_doc:12:chunk_0"
    assert "480" in first["content"]
    assert first["asset_id"] == 12
    assert first["asset_type"] == "plan_doc"
    assert first["chunk_index"] == 0
    # project_id 过滤下推到向量库
    assert fake_store.calls[0]["k"] == 5
    assert fake_store.calls[0]["filter"] is not None


async def test_knowledge_search_threshold_filters(client, monkeypatch) -> None:
    """低于默认阈值(0.5)的命中被过滤。"""
    pid = await _create_project(client, "项目A")
    fake_store = _FakeScoredStore([(_doc(0), 0.9), (_doc(1), 0.3)])
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: fake_store)

    r = await client.get(
        "/api/v1/assets/knowledge-search",
        params={"project_id": pid, "q": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["score"] == 0.9


async def test_knowledge_search_blank_query_422(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.get(
        "/api/v1/assets/knowledge-search",
        params={"project_id": pid, "q": "   "},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_knowledge_search_non_member_3030(client, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.get(
        "/api/v1/assets/knowledge-search",
        params={"project_id": pid, "q": "x"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_knowledge_search_vector_unavailable_4003(client, monkeypatch) -> None:
    """向量库异常 → BusinessError 4003（SRS 6.3，不泄漏底层错误）。"""
    pid = await _create_project(client, "项目A")

    class _BoomStore:
        async def asimilarity_search_with_relevance_scores(self, *a, **k):
            raise ConnectionError("qdrant down")

    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    monkeypatch.setattr(orch, "get_langchain_vector_store", lambda: _BoomStore())
    r = await client.get(
        "/api/v1/assets/knowledge-search",
        params={"project_id": pid, "q": "x"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 4003


# ---------- POST /reports/generate（Stage 4 占位） ----------


async def test_report_generate_requires_auth(client) -> None:
    r = await client.post("/api/v1/reports/generate", json={"run_no": "R1"})
    assert r.status_code == 401


async def test_report_generate_422_on_missing_run_no(client) -> None:
    r = await client.post("/api/v1/reports/generate", json={}, headers=_auth("a"))
    assert r.status_code == 422


async def test_report_generate_missing_run_2003(client) -> None:
    """run_no 不存在 → 2003（沿用执行可见性既有约定，先于成员判定）。"""
    r = await client.post(
        "/api/v1/reports/generate",
        json={"run_no": "R-MISSING"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003


async def test_report_generate_placeholder_shape(client, db_session) -> None:
    """Stage 5：成员可见 run → SRS 四字段响应壳（report_generator 降级生成）。"""
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260920099")
    r = await client.post(
        "/api/v1/reports/generate",
        json={"run_no": "R20260920099"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["run_no"] == "R20260920099"
    assert data["report_key"] == "reports/R20260920099/llm-report.md"
    # LLM 未配置时降级模板生成，confidence=0.5
    assert data["confidence"] == 0.5
    assert "降级" in data["notes"]
