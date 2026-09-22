"""P3 Stage 5 鲁棒性测试（SRS 8.1：异常输入优雅失败率 100%）。

覆盖维度：
- 空知识库（检索无结果）：返回降级 AnswerOutput，不抛异常
- LLM 调用异常（超时/网络）：返回降级 payload，citations 取 Top-1
- 极端输入：超长问题/纯空白/特殊字符/SQL 注入式输入
- 项目不存在/无权限：BusinessError 而非 500
- run_no 不存在：4004 而非 500
- SSE 流式中途异常：error 事件 + done 兜底，不中断连接

判定口径（SRS 8.1 鲁棒性 KPI）：
"优雅失败" = HTTP 不返回 500 且响应 JSON 合法（有 code/message/data 字段）
"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever

import app.services.llm.orchestrator as orch
from app.core.security import create_access_token
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


class FakeRetriever(BaseRetriever):
    docs: list[Document] = []

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return list(self.docs)


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_llm()
    reset_langchain_vector_store()
    yield
    reset_llm()
    reset_langchain_vector_store()


@pytest.fixture
def _llm_stub(monkeypatch):
    """标准 Mock：FakeListChatModel 返回合法 JSON + 假检索器有 1 条文档。"""
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    payload = {
        "answer": "TPS 480",
        "citations": ["plan_doc:12:chunk_0"],
        "confidence": 0.9,
        "notes": "",
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )

    async def _build(project_id, **kwargs):
        return FakeRetriever(
            docs=[
                Document(
                    page_content="chunk-0",
                    metadata={
                        "asset_id": 12,
                        "asset_type": "plan_doc",
                        "chunk_index": 0,
                        "project_id": project_id,
                    },
                )
            ]
        )

    monkeypatch.setattr(orch, "build_retriever", _build)


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username, "viewer")
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


# ---------- 极端输入 ----------


async def test_robustness_empty_message_rejected_422(client, _llm_stub) -> None:
    """纯空白消息 → 422（不触 LLM）。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "   "},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_robustness_max_length_message_accepted(client, _llm_stub) -> None:
    """恰好 2000 字符的消息 → 正常处理（边界值）。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "x" * 2000},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["answer"]


async def test_robustness_special_characters(client, _llm_stub) -> None:
    """特殊字符（emoji/CJK/引号/换行）→ 不 500。"""
    pid = await _create_project(client, "项目A")
    messages = [
        "性能如何？😀🎉",
        '含"引号"和{json}语法',
        "换行\n\t制表符",
        "SQL注入'; DROP TABLE--",
        "<script>alert('xss')</script>",
    ]
    for msg in messages:
        r = await client.post(
            "/api/v1/chat",
            json={"project_id": pid, "message": msg},
            headers=_auth("alice"),
        )
        assert r.status_code == 200, f"特殊字符消息失败: {msg!r} → {r.status_code}"
        assert r.json()["code"] == 0


async def test_robustness_negative_project_id_422(client, _llm_stub) -> None:
    """负 project_id → 422。"""
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": -1, "message": "hi"},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


# ---------- 空知识库 ----------


async def test_robustness_empty_knowledge_base(client, monkeypatch) -> None:
    """空知识库（检索无结果）：不抛异常，返回降级 AnswerOutput。"""
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())
    payload = {"answer": "知识库为空", "citations": [], "confidence": 0.5, "notes": ""}
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )

    async def _build(project_id, **kwargs):
        return FakeRetriever(docs=[])

    monkeypatch.setattr(orch, "build_retriever", _build)

    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "登录交易 TPS"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["citations"] == []
    assert data["answer"] == "知识库为空"


# ---------- LLM 调用异常 ----------


async def test_robustness_llm_timeout_degrades(client, monkeypatch) -> None:
    """LLM 调用超时 → 优雅降级（200 + 降级 AnswerOutput，不 500）。

    orchestrator 内部捕获 LLM 异常返回 fallback payload（SRS 1.5.4/NFR-02），
    故 HTTP 200 而非 503；503 仅在 run_qa_chain 自身外抛时触发。
    """
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())

    class TimeoutLLM(FakeListChatModel):
        async def ainvoke(self, *args, **kwargs):
            raise TimeoutError("llm timeout 30s")

    monkeypatch.setattr(orch, "get_llm", lambda: TimeoutLLM(responses=["x"]))

    async def _build(project_id, **kwargs):
        return FakeRetriever(
            docs=[
                Document(
                    page_content="x",
                    metadata={
                        "asset_id": 1,
                        "asset_type": "plan_doc",
                        "chunk_index": 0,
                        "project_id": project_id,
                    },
                )
            ]
        )

    monkeypatch.setattr(orch, "build_retriever", _build)

    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200  # 优雅降级，非 500
    data = r.json()["data"]
    assert "降级" in data["answer"]
    assert "LLM 调用失败" in data["notes"]
    assert data["citations"] == ["plan_doc:1:chunk_0"]  # Top-1 兜底


async def test_robustness_llm_connection_error_degrades(client, monkeypatch) -> None:
    """LLM 网络异常 → 优雅降级（200 + 降级 AnswerOutput，不 500）。"""
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())

    class NetErrLLM(FakeListChatModel):
        async def ainvoke(self, *args, **kwargs):
            raise ConnectionError("network unreachable")

    monkeypatch.setattr(orch, "get_llm", lambda: NetErrLLM(responses=["x"]))

    async def _build(project_id, **kwargs):
        return FakeRetriever(
            docs=[
                Document(
                    page_content="x",
                    metadata={
                        "asset_id": 1,
                        "asset_type": "plan_doc",
                        "chunk_index": 0,
                        "project_id": project_id,
                    },
                )
            ]
        )

    monkeypatch.setattr(orch, "build_retriever", _build)

    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200  # 优雅降级，非 500
    assert "降级" in r.json()["data"]["answer"]


# ---------- 检索基础设施异常 ----------


async def test_robustness_retrieval_failure_degrades(client, monkeypatch) -> None:
    """向量库不可用 → 503 + 降级 JSON（同步 /chat 路径）。"""
    monkeypatch.setattr(orch, "get_settings", lambda: _stub_settings())

    async def _boom(*args, **kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(orch, "build_retriever", _boom)
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=['{"answer":"a","citations":[]}']),
    )

    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 503
    assert r.json()["code"] == 4000


# ---------- SSE 流式鲁棒性 ----------


async def test_robustness_sse_retrieval_failure_sends_error_and_done(
    client, db_env, monkeypatch, _llm_stub
) -> None:
    """SSE 流式中检索异常 → error 事件 + done 兜底，不中断连接。"""
    import app.api.v1.chat as chat_api

    monkeypatch.setattr(chat_api, "SessionLocal", db_env)
    pid = await _create_project(client, "项目A")

    # 让 astream_qa_events 内部检索失败
    async def _boom_retriever(*args, **kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(orch, "build_retriever", _boom_retriever)

    async with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"project_id": pid, "message": "q"},
        headers=_auth("alice"),
    ) as resp:
        assert resp.status_code == 200
        events = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "done"
    assert events[-1]["final"]["answer"]  # 兜底回答非空


# ---------- 项目门禁鲁棒性 ----------


async def test_robustness_nonexistent_project_3021(client, _llm_stub) -> None:
    """项目不存在 → 3021（非 500）。"""
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": 99999, "message": "q"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3021


async def test_robustness_non_member_3030(client, _llm_stub) -> None:
    """非项目成员 → 3030（非 500）。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_robustness_run_no_missing_4004(client, _llm_stub) -> None:
    """run_no 不存在 → 4004（非 500）。"""
    pid = await _create_project(client, "项目A")
    r = await client.post(
        "/api/v1/chat",
        json={"project_id": pid, "message": "q", "run_no": "R-MISSING"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 4004


# ---------- reports/generate 鲁棒性 ----------


async def test_robustness_report_generate_missing_run_2003(client, _llm_stub) -> None:
    """reports/generate 不存在 run_no → 2003（非 500）。"""
    r = await client.post(
        "/api/v1/reports/generate",
        json={"run_no": "R-MISSING"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003
