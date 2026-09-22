"""P3 Stage 4 LLM 对话 WebSocket 测试（FR-10，SRS 6.2：/ws/chat）。

覆盖：
- 握手：坏 token → 1008 拒绝（不触库）
- 正向：成员发 {project_id,message} → token* → done（done.final 五字段）
- 非成员 → error 事件 + 1008 关闭
- run_no 不存在 → error.code=4004 + 1008
- 非法上行帧 → error 事件但连接保持，随后可正常问答（多轮/容错）

跨事件循环说明：starlette TestClient 在独立 portal 循环跑 ASGI 应用，
故 WS 侧的 DB 用文件型 sqlite + NullPool（每循环各开连接），不能复用
conftest 的 StaticPool 内存库 maker（单连接绑定测试主循环）。
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import WebSocketDisconnect
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

import app.services.llm.orchestrator as orch
import app.ws.chat as ws_chat
from app.core.security import create_access_token
from app.main import app
from app.models.base import Base
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.services.llm.client import reset_llm
from app.services.vector_store import reset_langchain_vector_store


def _token(username: str, role: str = "viewer") -> str:
    return create_access_token(username, role)


def _doc(i: int) -> Document:
    return Document(
        page_content=f"登录交易 TPS 480 片段{i}",
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


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_llm()
    reset_langchain_vector_store()
    yield
    reset_llm()
    reset_langchain_vector_store()


@pytest_asyncio.fixture
async def ws_db(tmp_path, monkeypatch):
    """文件型 sqlite + NullPool：种子在主循环写，portal 循环各开连接。"""
    db_file = tmp_path / "ws_chat.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        session.add_all(
            [
                Project(name="项目A"),
                ProjectMember(
                    project_id=1,
                    username="alice",
                    role="owner",
                    granted_by="system",
                ),
            ]
        )
        await session.commit()

    monkeypatch.setattr(ws_chat, "SessionLocal", maker)
    yield maker
    await engine.dispose()


def _patch_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        orch,
        "get_settings",
        lambda: SimpleNamespace(
            top_k=5,
            similarity_threshold=0.5,
            use_bm25=False,
            max_context_chars=1800,
        ),
    )
    answer = {
        "answer": "登录交易基准 TPS 为 480 [plan_doc:12:chunk_0]",
        "citations": ["plan_doc:12:chunk_0"],
        "used_metrics": ["tps"],
        "confidence": 0.9,
        "notes": "",
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(answer, ensure_ascii=False)]),
    )

    async def _build(project_id, **kwargs):
        return FakeRetriever(docs=[_doc(0)])

    monkeypatch.setattr(orch, "build_retriever", _build)


def _drain_until_done(ws) -> list[dict]:
    """持续收事件直到 done（断言终态必达）。"""
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "done":
            return events


# ---------- 握手 ----------


def test_ws_chat_bad_token_rejected_1008() -> None:
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect("/ws/chat?token=bad") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_chat_missing_token_rejected_1008() -> None:
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect("/ws/chat") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


# ---------- 正向问答流 ----------


async def test_ws_chat_member_receives_token_and_done(ws_db, monkeypatch) -> None:
    _patch_llm(monkeypatch)
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/chat?token={_token('alice')}") as ws:
        ws.send_json({"project_id": 1, "message": "登录交易 TPS 如何"})
        events = _drain_until_done(ws)

        # 同连接多轮：再发一轮仍能拿到 token/done（get_llm 每次返回新 Fake 实例）
        ws.send_json({"project_id": 1, "message": "再问一次"})
        events2 = _drain_until_done(ws)

    types = [e["type"] for e in events]
    assert types[0] == "token"
    assert types[-1] == "done"
    final = events[-1]["final"]
    assert set(final.keys()) == {
        "answer",
        "citations",
        "used_metrics",
        "confidence",
        "notes",
    }
    assert final["citations"] == ["plan_doc:12:chunk_0"]
    assert final["confidence"] == 0.9
    assert events2[0]["type"] == "token"
    assert events2[-1]["type"] == "done"


# ---------- 门禁 ----------


async def test_ws_chat_non_member_error_then_1008(ws_db, monkeypatch) -> None:
    _patch_llm(monkeypatch)
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(f"/ws/chat?token={_token('bob')}") as ws:
            ws.send_json({"project_id": 1, "message": "q"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert "code" not in err  # 非成员不透传内部业务码
            ws.receive_json()
    assert exc.value.code == 1008


async def test_ws_chat_missing_run_no_returns_4004(ws_db, monkeypatch) -> None:
    _patch_llm(monkeypatch)
    tc = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(f"/ws/chat?token={_token('alice')}") as ws:
            ws.send_json({"project_id": 1, "message": "q", "run_no": "R-MISSING"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["code"] == 4004
            ws.receive_json()
    assert exc.value.code == 1008


# ---------- 上行容错 ----------


async def test_ws_chat_invalid_frame_keeps_connection_alive(ws_db, monkeypatch) -> None:
    _patch_llm(monkeypatch)
    tc = TestClient(app)
    with tc.websocket_connect(f"/ws/chat?token={_token('alice')}") as ws:
        # 非 JSON 对象
        ws.send_json(["not", "object"])
        err = ws.receive_json()
        assert err["type"] == "error"

        # 缺 message
        ws.send_json({"project_id": 1})
        assert ws.receive_json()["type"] == "error"

        # 纯空白 message
        ws.send_json({"project_id": 1, "message": "   "})
        assert ws.receive_json()["type"] == "error"

        # top_k 越界
        ws.send_json({"project_id": 1, "message": "ok", "top_k": 99})
        assert ws.receive_json()["type"] == "error"

        # 连接仍然有效：合法消息正常收尾
        ws.send_json({"project_id": 1, "message": "登录交易 TPS"})
        events = _drain_until_done(ws)
        assert events[-1]["final"]["answer"]
