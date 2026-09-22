"""P3 Stage 3 FR-08 指标校验 Guardrail 测试（SRS FR-08 + 附录 A）。

覆盖验收用例：
- TC-VLD-001：差异 >5% → notes 写 "Mismatch with actual metrics: {field}"，
  confidence 压至 ≤ 0.7
- TC-VLD-002：差异 ≤5% → notes 标注 "Metrics verified"
- 附录 A JSON 示例可复现；±5% 边界、零基准、错误率百分号/比率口径、
  run_no 无基准、数值无法解析（N/A）、RunnableLambda 封装、es_client
  标准化方法、orchestrator.run_qa_chain(run_no=...) 端到端接入。

打桩范式：纯函数 apply_metric_guardrail 无需 ES；异步路径 monkeypatch
guardrail.es_client.get_run_metrics / es_client.query_summary 等。
"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever

import app.services.es_client as es_client
import app.services.llm.guardrail as guard
import app.services.llm.orchestrator as orch
from app.services.llm.client import AnswerOutput, reset_llm
from app.services.vector_store import reset_langchain_vector_store

TOL = 0.05


# ---------- 公共夹具/桩（沿 test_llm_orchestrator 范式） ----------


def _stub_settings() -> SimpleNamespace:
    return SimpleNamespace(
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


def _doc(i: int) -> Document:
    return Document(
        page_content=f"chunk-{i} 压测知识库内容",
        metadata={
            "asset_id": 12,
            "asset_type": "plan_doc",
            "chunk_index": i,
            "project_id": 1,
        },
    )


class _FakeRetriever(BaseRetriever):
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
def stub_settings(monkeypatch):
    s = _stub_settings()
    monkeypatch.setattr(orch, "get_settings", lambda: s)
    return s


def _patch_retriever(monkeypatch, docs: list[Document]) -> None:
    async def fake_build_retriever(project_id, **kwargs):
        return _FakeRetriever(docs=docs)

    monkeypatch.setattr(orch, "build_retriever", fake_build_retriever)


def _payload(answer: str, **overrides) -> dict:
    base = {
        "answer": answer,
        "citations": [],
        "used_metrics": None,
        "confidence": 0.9,
        "notes": "",
    }
    base.update(overrides)
    return base


def _actual(tps=0.0, p95_ms=0.0, error_rate=0.0, samples=1000) -> dict:
    return {
        "tps": tps,
        "p95_ms": p95_ms,
        "error_rate": error_rate,
        "samples": samples,
        "source": "summary",
    }


# ---------- FR-08 数值抽取（正则归一化） ----------


def test_extract_appendix_a_sentence() -> None:
    """附录 A 原句：TPS/P95/错误率（百分号）三字段全抽取。"""
    found = guard.extract_metric_values(
        "本次压测 TPS 为 480，P95 为 120ms，错误率 0.5%"
    )
    assert found["tps"].value == 480.0
    assert found["p95_ms"].value == 120.0
    assert found["error_rate"].value == 0.5
    assert found["error_rate"].percent is True


def test_extract_error_rate_ratio_form() -> None:
    """无百分号小数（≤1）按比率 ×100：0.005 → 0.5%。"""
    found = guard.extract_metric_values("错误率为0.005")
    assert found["error_rate"].value == 0.5
    assert found["error_rate"].percent is False


def test_extract_error_rate_plain_percent_number() -> None:
    """无百分号但 >1 按百分数："错误率 1.2" → 1.2。"""
    assert guard.extract_metric_values("错误率 1.2")["error_rate"].value == 1.2


def test_extract_tps_number_before_keyword() -> None:
    """数值前置形式："压到 500 TPS" / "480 笔/秒"。"""
    assert guard.extract_metric_values("登录交易已压到 500 TPS")["tps"].value == 500.0
    assert guard.extract_metric_values("当前 480 笔/秒")["tps"].value == 480.0


def test_extract_p95_variants() -> None:
    assert guard.extract_metric_values("p95_rt=120.5")["p95_ms"].value == 120.5
    assert guard.extract_metric_values("P95响应时间：120ms")["p95_ms"].value == 120.0
    assert guard.extract_metric_values("P95延迟 230 毫秒")["p95_ms"].value == 230.0


def test_extract_p95_percentile_parenthesis_not_95() -> None:
    """解释性括号 "P95（95分位）约 120ms" 不得误抽 95。"""
    found = guard.extract_metric_values("P95（95分位）约 120ms")
    assert found["p95_ms"].value == 120.0


def test_extract_english_error_rate() -> None:
    found = guard.extract_metric_values("error rate 1.2%, err_rate=2%")
    assert found["error_rate"].value == 1.2  # 取首个出现


def test_extract_no_metrics_returns_empty() -> None:
    assert guard.extract_metric_values("已为您创建登录交易压测场景") == {}


def test_find_unparseable_explicit_placeholder() -> None:
    """关键词 + 显式占位（未知/N/A/暂无）→ N/A 字段。"""
    fields = guard._find_unparseable_fields("TPS 未知，稍后给出", {})
    assert fields == ["tps"]
    fields = guard._find_unparseable_fields("错误率暂无数据", {})
    assert fields == ["error_rate"]


def test_find_unparseable_ignores_field_name_listing() -> None:
    """字段名罗列 "tps/p95_ms/error_rate 三个指标" 不得误报 N/A。"""
    fields = guard._find_unparseable_fields(
        "回答涉及 tps/p95_ms/error_rate 三个指标字段", {}
    )
    assert fields == []


# ---------- TC-VLD-001：超差降 confidence + mismatch notes ----------


def test_tc_vld_001_mismatch_caps_confidence_and_notes() -> None:
    """差异 10%（480 vs 534）> 5% → confidence ≤ 0.7 + mismatch notes。"""
    payload = _payload("本次压测 TPS 为 480，P95 为 120ms")
    out = guard.apply_metric_guardrail(
        payload, _actual(tps=534.0, p95_ms=120.0), run_no="1001", tolerance=TOL
    )
    assert "Mismatch with actual metrics: tps" in out["notes"]
    assert out["confidence"] <= 0.7
    assert out["confidence"] == 0.7  # min(0.9, 0.7)
    # used_metrics 回填 + run_summary 引用追加（附录 A citations 范式）
    assert out["used_metrics"] == ["tps", "p95_ms"]
    assert "run_summary:1001" in out["citations"]


def test_appendix_a_example_reproducible() -> None:
    """SRS 附录 A JSON 示例可复现：五字段、mismatch tps、confidence ≤ 0.7。"""
    payload = {
        "answer": "本次压测 TPS 为 480，P95 为 120ms",
        "citations": ["run_summary:1001", "metrics:1001:p95"],
        "used_metrics": ["tps", "p95_ms"],
        "notes": "",
        "confidence": 0.9,
    }
    out = guard.apply_metric_guardrail(
        payload, _actual(tps=534.0, p95_ms=120.0), run_no="1001", tolerance=TOL
    )
    assert set(out.keys()) >= {
        "answer",
        "citations",
        "used_metrics",
        "confidence",
        "notes",
    }
    assert out["notes"] == "Mismatch with actual metrics: tps"
    assert out["confidence"] <= 0.7
    assert out["citations"] == ["run_summary:1001", "metrics:1001:p95"]  # 引用去重
    assert out["answer"] == payload["answer"]


def test_mismatch_confidence_already_low_stays() -> None:
    """原 confidence 已低于 0.7 时不再抬高。"""
    payload = _payload("TPS 为 900", confidence=0.6, notes="已有备注")
    out = guard.apply_metric_guardrail(payload, _actual(tps=500.0), run_no="R1")
    assert out["confidence"] == 0.6
    assert out["notes"] == "已有备注; Mismatch with actual metrics: tps"


# ---------- TC-VLD-002：容差内一致 ----------


def test_tc_vld_002_within_tolerance_verified() -> None:
    """差异 ≤5% → "Metrics verified"，confidence 不动。"""
    payload = _payload("TPS 为 498，P95 为 121ms")
    out = guard.apply_metric_guardrail(
        payload, _actual(tps=500.0, p95_ms=120.0), run_no="R1", tolerance=TOL
    )
    assert "Metrics verified" in out["notes"]
    assert "Mismatch" not in out["notes"]
    assert out["confidence"] == 0.9
    assert "run_summary:R1" in out["citations"]


def test_tolerance_boundary_inclusive() -> None:
    """恰 5%（100 vs 105）视为一致；略超（105.1）判超差。"""
    ok = guard.apply_metric_guardrail(
        _payload("TPS 为 105"), _actual(tps=100.0), run_no="R1", tolerance=TOL
    )
    assert "Metrics verified" in ok["notes"]
    bad = guard.apply_metric_guardrail(
        _payload("TPS 为 105.1"), _actual(tps=100.0), run_no="R1", tolerance=TOL
    )
    assert "Mismatch with actual metrics: tps" in bad["notes"]


def test_error_rate_percent_and_ratio_equivalent() -> None:
    """错误率：百分号 0.5% 与比率 0.005 对同一基准（0.5%）均判一致。"""
    for answer in ("错误率 0.5%", "错误率 0.005"):
        out = guard.apply_metric_guardrail(
            _payload(answer), _actual(error_rate=0.5), run_no="R1", tolerance=TOL
        )
        assert "Metrics verified" in out["notes"], answer
    over = guard.apply_metric_guardrail(
        _payload("错误率 2%"), _actual(error_rate=0.5), run_no="R1", tolerance=TOL
    )
    assert "Mismatch with actual metrics: error_rate" in over["notes"]


def test_zero_actual_metric_handling() -> None:
    """基准为 0：零值一致，非零直接判超差（防除零）。"""
    zero = guard.apply_metric_guardrail(
        _payload("TPS 为 0"), _actual(tps=0.0), run_no="R1", tolerance=TOL
    )
    assert "Metrics verified" in zero["notes"]
    nonzero = guard.apply_metric_guardrail(
        _payload("TPS 为 10"), _actual(tps=0.0), run_no="R1", tolerance=TOL
    )
    assert "Mismatch with actual metrics: tps" in nonzero["notes"]
    assert nonzero["confidence"] <= 0.7


# ---------- 异常处理：无基准 / N/A / 无指标回答 ----------


def test_no_baseline_skips_with_note_when_metrics_mentioned() -> None:
    """run_no 无基准数据 → 跳过校验，notes 标注"无法获取基准指标"。"""
    payload = _payload("本次压测 TPS 为 480")
    out = guard.apply_metric_guardrail(payload, None, run_no="R-NOPE", tolerance=TOL)
    assert "无法获取基准指标" in out["notes"]
    assert "R-NOPE" in out["notes"]
    assert out["confidence"] == 0.9  # 不压置信度


def test_no_baseline_silent_when_answer_has_no_metrics() -> None:
    """回答与指标无关且无基准时不噪声标注。"""
    out = guard.apply_metric_guardrail(
        _payload("已为您创建场景"), None, run_no="R1", tolerance=TOL
    )
    assert out["notes"] == ""


def test_unparseable_value_marked_na_with_baseline() -> None:
    """基准存在但回答给占位符 → N/A + 日志，其余字段照常校验。"""
    out = guard.apply_metric_guardrail(
        _payload("TPS 未知，P95 为 120ms"),
        _actual(tps=500.0, p95_ms=120.0),
        run_no="R1",
        tolerance=TOL,
    )
    assert "指标 tps 数值无法解析，标记 N/A" in out["notes"]
    assert "Metrics verified" in out["notes"]  # p95 仍一致


def test_input_payload_not_mutated() -> None:
    payload = _payload("TPS 为 900")
    guard.apply_metric_guardrail(payload, _actual(tps=500.0), run_no="R1")
    assert payload["confidence"] == 0.9
    assert payload["notes"] == ""


# ---------- 异步入口 validate_metrics / Runnable / AnswerOutput ----------


async def test_validate_metrics_fetches_es_baseline(monkeypatch) -> None:
    async def fake_get(run_no: str) -> dict:
        assert run_no == "R1"
        return _actual(tps=500.0)

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_get)
    out = await guard.validate_metrics("R1", _payload("TPS 为 498"), tolerance=TOL)
    assert "Metrics verified" in out["notes"]


async def test_validate_metrics_none_baseline(monkeypatch) -> None:
    async def fake_get(run_no: str) -> None:
        return None

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_get)
    out = await guard.validate_metrics("R2", _payload("TPS 为 480"), tolerance=TOL)
    assert "无法获取基准指标" in out["notes"]


async def test_validate_metrics_es_error_degrades_to_skip(monkeypatch) -> None:
    """ES 异常不外抛，按无基准跳过（NFR-02）。"""

    async def boom(run_no: str):
        raise RuntimeError("es down")

    monkeypatch.setattr(guard.es_client, "get_run_metrics", boom)
    out = await guard.validate_metrics("R3", _payload("TPS 为 480"), tolerance=TOL)
    assert "无法获取基准指标" in out["notes"]
    assert out["confidence"] == 0.9


async def test_build_metric_guardrail_runnable(monkeypatch) -> None:
    """FR-08 RunnableLambda 封装可 ainvoke（SRS 2.3 能力映射）。"""

    async def fake_get(run_no: str) -> dict:
        return _actual(tps=500.0)

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_get)
    runnable = guard.build_metric_guardrail("R1", tolerance=TOL)
    out = await runnable.ainvoke(_payload("TPS 为 470"))
    assert "Mismatch with actual metrics: tps" in out["notes"]
    assert out["confidence"] <= 0.7


async def test_validate_answer_returns_model(monkeypatch) -> None:
    async def fake_get(run_no: str) -> dict:
        return _actual(tps=500.0)

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_get)
    answer = AnswerOutput(answer="TPS 为 498", confidence=0.8)
    out = await guard.validate_answer("R1", answer, tolerance=TOL)
    assert isinstance(out, AnswerOutput)
    assert "Metrics verified" in out.notes
    assert out.confidence == 0.8


# ---------- es_client.get_run_metrics 标准化方法 ----------


async def test_get_run_metrics_prefers_terminal_summary(monkeypatch) -> None:
    summary_doc = {
        "agents": ["a1"],
        "summary": {
            "samples": 200,
            "success": 198,
            "errors": 2,
            "p95_rt": 123.4,
            "avg_tps": 45.6,
        },
    }

    async def fake_summary(run_no: str):
        return summary_doc

    async def fail_realtime(run_no: str):
        raise AssertionError("终态存在时不应查实时聚合")

    monkeypatch.setattr(es_client, "query_summary", fake_summary)
    monkeypatch.setattr(es_client, "query_realtime_summary", fail_realtime)

    metrics = await es_client.get_run_metrics("R1")
    assert metrics == {
        "tps": 45.6,
        "p95_ms": 123.4,
        "error_rate": 1.0,  # 2/200*100
        "samples": 200,
        "source": "summary",
    }


async def test_get_run_metrics_falls_back_to_realtime(monkeypatch) -> None:
    async def none_summary(run_no: str) -> None:
        return None

    async def fake_realtime(run_no: str) -> dict:
        return {"samples": 100, "errors": 5, "p95_rt": 88.0, "avg_tps": 30.0}

    monkeypatch.setattr(es_client, "query_summary", none_summary)
    monkeypatch.setattr(es_client, "query_realtime_summary", fake_realtime)

    metrics = await es_client.get_run_metrics("R-RUNNING")
    assert metrics is not None
    assert metrics["source"] == "realtime"
    assert metrics["tps"] == 30.0
    assert metrics["p95_ms"] == 88.0
    assert metrics["error_rate"] == 5.0


async def test_get_run_metrics_none_when_no_data(monkeypatch) -> None:
    async def none_summary(run_no: str) -> None:
        return None

    async def empty_realtime(run_no: str) -> dict:
        return {
            "samples": 0,
            "errors": 0,
            "p95_rt": 0.0,
            "avg_tps": 0.0,
            "by_label": [],
        }

    monkeypatch.setattr(es_client, "query_summary", none_summary)
    monkeypatch.setattr(es_client, "query_realtime_summary", empty_realtime)
    assert await es_client.get_run_metrics("R-MISSING") is None


# ---------- orchestrator.run_qa_chain(run_no=...) 端到端接入 ----------


async def test_run_qa_chain_run_no_applies_guardrail(
    stub_settings, monkeypatch
) -> None:
    """run_no 非空 → LLM 结果经 FR-08 校验：超差压 confidence + mismatch notes。"""
    payload = {
        "answer": "本次压测 TPS 为 480，P95 为 120ms",
        "citations": [],
        "used_metrics": ["tps", "p95_ms"],
        "confidence": 0.9,
        "notes": "",
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0)])

    async def fake_metrics(run_no: str) -> dict:
        assert run_no == "R-1"
        return _actual(tps=534.0, p95_ms=120.0)

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_metrics)

    result = await orch.run_qa_chain(1, "R-1 这次压测结果如何", run_no="R-1")
    assert isinstance(result, AnswerOutput)
    assert "Mismatch with actual metrics: tps" in result.notes
    assert result.confidence <= 0.7
    assert "run_summary:R-1" in result.citations


async def test_run_qa_chain_run_no_verified(stub_settings, monkeypatch) -> None:
    payload = {
        "answer": "TPS 为 498，P95 为 120ms，错误率 0.5%",
        "citations": [],
        "used_metrics": None,
        "confidence": 0.85,
        "notes": "",
    }
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0)])

    async def fake_metrics(run_no: str) -> dict:
        return _actual(tps=500.0, p95_ms=120.0, error_rate=0.5)

    monkeypatch.setattr(guard.es_client, "get_run_metrics", fake_metrics)

    result = await orch.run_qa_chain(1, "q", run_no="R-1")
    assert "Metrics verified" in result.notes
    assert result.confidence == 0.85
    assert result.used_metrics == ["tps", "p95_ms", "error_rate"]


async def test_run_qa_chain_without_run_no_skips_es(stub_settings, monkeypatch) -> None:
    """不传 run_no → 完全不触 ES（Stage 2 既有行为不变）。"""
    payload = {"answer": "TPS 为 480", "citations": [], "confidence": 0.9, "notes": ""}
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0)])

    async def boom(run_no: str):
        raise AssertionError("未传 run_no 不应查询基准指标")

    monkeypatch.setattr(guard.es_client, "get_run_metrics", boom)

    result = await orch.run_qa_chain(1, "q")
    assert "Mismatch" not in result.notes
    assert "无法获取基准指标" not in result.notes


async def test_run_qa_chain_guardrail_exception_returns_raw(
    stub_settings, monkeypatch
) -> None:
    """校验自身异常不外溢，返回未校验原始结果（NFR-02）。"""
    payload = {"answer": "TPS 为 480", "citations": [], "confidence": 0.9, "notes": ""}
    monkeypatch.setattr(
        orch,
        "get_llm",
        lambda: FakeListChatModel(responses=[json.dumps(payload, ensure_ascii=False)]),
    )
    _patch_retriever(monkeypatch, [_doc(0)])

    async def boom_validate(run_no, answer, tolerance=None):
        raise RuntimeError("guardrail broken")

    monkeypatch.setattr(orch, "validate_answer", boom_validate)

    result = await orch.run_qa_chain(1, "q", run_no="R1")
    assert result.answer == "TPS 为 480"
    assert result.confidence == 0.9
