"""P3 Stage 5 报告生成器测试（report_generator.py）。

覆盖：
- LLM 不可用时降级模板生成（含 4 章节 + 数据填充）
- LLM 可用时调用 LLM 生成（FakeListChatModel 打桩）
- ES 异常不阻断：报告中标注"数据不可用"
- MinIO 写入异常不阻断：notes 标注失败
- 报告写入 MinIO 的 key 为 `reports/{run_no}/llm-report.md`
"""

from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

import app.services.llm.report_generator as rg
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services.llm.client import reset_llm
from app.services.vector_store import reset_langchain_vector_store


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_llm()
    reset_langchain_vector_store()
    yield
    reset_llm()
    reset_langchain_vector_store()


@pytest.fixture
def _no_llm(monkeypatch):
    """LLM 未配置（FakeListChatModel 降级）→ 走模板路径。"""
    monkeypatch.setattr(rg, "get_llm", lambda: FakeListChatModel(responses=["x"]))


@pytest.fixture
def _real_llm(monkeypatch):
    """LLM 已配置（非 FakeListChatModel）→ 走 LLM 调用路径。"""

    # 打桩 ainvoke 返回 Markdown
    class _MockLLM:
        async def ainvoke(self, prompt):
            class Resp:
                content = "# LLM 报告\n\n## 1. 执行概览\nTPS=480"

            return Resp()

    monkeypatch.setattr(rg, "get_llm", lambda: _MockLLM())


async def _seed_run(db_session, run_no: str = "R20260920001") -> int:
    """创建项目+场景+执行记录，返回 project_id。"""
    project = Project(name="项目A")
    db_session.add(project)
    await db_session.flush()
    pid = project.id
    db_session.add(
        ProjectMember(project_id=pid, username="alice", role="owner", granted_by="t")
    )
    db_session.add(Scenario(project_id=pid, name="场景1"))
    await db_session.flush()
    db_session.add(
        ScenarioRun(run_no=run_no, scenario_id=1, agent_ids=["a1"], expected_results=2)
    )
    await db_session.commit()
    return pid


# ---------- 降级模板路径 ----------


async def test_generate_report_fallback_template(
    client, db_session, _no_llm, monkeypatch
) -> None:
    """LLM 不可用 → 降级模板生成，含 4 章节 + ES 数据填充。"""
    await _seed_run(db_session)

    # 打桩 ES 返回基准指标
    async def _metrics(run_no):
        return {
            "tps": 480.0,
            "p95_ms": 120.0,
            "error_rate": 0.5,
            "samples": 1000,
            "source": "summary",
        }

    async def _summary(run_no):
        return {
            "samples": 1000,
            "success": 995,
            "errors": 5,
            "avg_tps": 480.0,
            "p95_rt": 120.0,
        }

    monkeypatch.setattr(rg.es_client, "get_run_metrics", _metrics)
    monkeypatch.setattr(rg.es_client, "query_summary", _summary)

    # 打桩 MinIO 上传
    uploaded: list[tuple[str, bytes]] = []

    async def _upload(key, data, content_type=""):
        uploaded.append((key, data))

    monkeypatch.setattr(rg.storage, "upload_bytes", _upload)

    result = await rg.generate_report("R20260920001", db_session)
    assert result["run_no"] == "R20260920001"
    assert result["report_key"] == "reports/R20260920001/llm-report.md"
    assert result["confidence"] == 0.5
    assert "LLM 未配置" in result["notes"]
    # 验证 MinIO 写入
    assert len(uploaded) == 1
    key, data = uploaded[0]
    assert key == "reports/R20260920001/llm-report.md"
    md = data.decode("utf-8")
    assert "## 1. 执行概览" in md
    assert "## 2. 性能指标" in md
    assert "## 3. 资源使用" in md
    assert "## 4. 结论与建议" in md
    assert "480" in md
    assert "120" in md


async def test_generate_report_fallback_no_es_data(
    client, db_session, _no_llm, monkeypatch
) -> None:
    """ES 无数据 → 报告标注"数据不可用"，仍正常生成。"""
    await _seed_run(db_session)

    monkeypatch.setattr(rg.es_client, "get_run_metrics", AsyncMock(return_value=None))
    monkeypatch.setattr(rg.es_client, "query_summary", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rg.es_client, "query_realtime_summary", AsyncMock(return_value={})
    )
    monkeypatch.setattr(rg.storage, "upload_bytes", AsyncMock())

    result = await rg.generate_report("R20260920001", db_session)
    assert result["confidence"] == 0.5
    assert result["report_key"] == "reports/R20260920001/llm-report.md"


async def test_generate_report_minio_failure_not_blocking(
    client, db_session, _no_llm, monkeypatch
) -> None:
    """MinIO 写入失败 → notes 标注，不阻断 API 响应。"""
    await _seed_run(db_session)

    monkeypatch.setattr(rg.es_client, "get_run_metrics", AsyncMock(return_value=None))
    monkeypatch.setattr(rg.es_client, "query_summary", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rg.es_client, "query_realtime_summary", AsyncMock(return_value={})
    )

    async def _fail(*a, **kw):
        raise ConnectionError("minio down")

    monkeypatch.setattr(rg.storage, "upload_bytes", _fail)

    result = await rg.generate_report("R20260920001", db_session)
    assert "MinIO 写入失败" in result["notes"]


# ---------- LLM 生成路径 ----------


async def test_generate_report_llm_path(
    client, db_session, _real_llm, monkeypatch
) -> None:
    """LLM 已配置 → 调用 LLM 生成报告，confidence=0.85。"""
    await _seed_run(db_session)

    monkeypatch.setattr(rg.es_client, "get_run_metrics", AsyncMock(return_value=None))
    monkeypatch.setattr(rg.es_client, "query_summary", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rg.es_client, "query_realtime_summary", AsyncMock(return_value={})
    )
    monkeypatch.setattr(rg.storage, "upload_bytes", AsyncMock())

    result = await rg.generate_report("R20260920001", db_session)
    assert result["confidence"] == 0.85
    assert "LLM 生成报告" in result["notes"]


async def test_generate_report_llm_failure_falls_back(
    client, db_session, monkeypatch
) -> None:
    """LLM 调用异常 → 降级模板，notes 标注失败。"""
    await _seed_run(db_session)

    class _BoomLLM:
        async def ainvoke(self, prompt):
            raise RuntimeError("llm 500")

    monkeypatch.setattr(rg, "get_llm", lambda: _BoomLLM())
    monkeypatch.setattr(rg.es_client, "get_run_metrics", AsyncMock(return_value=None))
    monkeypatch.setattr(rg.es_client, "query_summary", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rg.es_client, "query_realtime_summary", AsyncMock(return_value={})
    )
    monkeypatch.setattr(rg.storage, "upload_bytes", AsyncMock())

    result = await rg.generate_report("R20260920001", db_session)
    assert result["confidence"] == 0.5
    assert "LLM 调用失败" in result["notes"]
