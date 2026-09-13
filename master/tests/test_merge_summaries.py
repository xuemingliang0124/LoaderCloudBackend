"""_merge_summaries 合并口径单测：按 (label, sample_type) 聚合。"""

from app.services.orchestrator import _merge_summaries


def test_same_label_request_and_transaction_not_merged():
    """事务与取样器同名：sample_type 不同，分桶互不污染。"""
    summary = {
        "samples": 3,
        "errors": 0,
        "p95_rt": 100.0,
        "max_tps": 2.0,
        "by_label": [
            {
                "label": "下单",
                "sample_type": "request",
                "samples": 2,
                "errors": 0,
                "p95_rt": 90.0,
                "max_tps": 2.0,
            },
            {
                "label": "下单",
                "sample_type": "transaction",
                "samples": 1,
                "errors": 0,
                "p95_rt": 200.0,
                "max_tps": 1.0,
            },
        ],
    }
    merged = _merge_summaries([summary])
    assert merged["samples"] == 3
    items = {(x["label"], x["sample_type"]): x for x in merged["by_label"]}
    assert len(items) == 2
    assert items[("下单", "request")]["samples"] == 2
    assert items[("下单", "transaction")]["p95_rt"] == 200.0


def test_legacy_summary_without_sample_type_defaults_request():
    """旧 Agent/历史分片缺 sample_type 时按 request 兜底合并。"""
    legacy = {
        "samples": 1,
        "errors": 0,
        "p95_rt": 10.0,
        "max_tps": 1.0,
        "by_label": [
            {"label": "ping", "samples": 1, "errors": 0, "p95_rt": 10.0, "max_tps": 1.0}
        ],
    }
    merged = _merge_summaries([legacy])
    assert merged["by_label"][0]["sample_type"] == "request"


def test_accumulate_across_agents_by_type():
    """多 Agent 同 (label, sample_type) 累加，p95/max_tps 取 max。"""
    s1 = {
        "samples": 2,
        "errors": 1,
        "p95_rt": 100.0,
        "max_tps": 2.0,
        "by_label": [
            {
                "label": "login",
                "sample_type": "request",
                "samples": 2,
                "errors": 1,
                "p95_rt": 100.0,
                "max_tps": 2.0,
            }
        ],
    }
    s2 = {
        "samples": 2,
        "errors": 0,
        "p95_rt": 300.0,
        "max_tps": 3.0,
        "by_label": [
            {
                "label": "login",
                "sample_type": "request",
                "samples": 2,
                "errors": 0,
                "p95_rt": 300.0,
                "max_tps": 3.0,
            }
        ],
    }
    merged = _merge_summaries([s1, s2])
    assert merged["samples"] == 4
    assert merged["errors"] == 1
    assert merged["p95_rt"] == 300.0
    assert merged["max_tps"] == 3.0
    assert len(merged["by_label"]) == 1
    bucket = merged["by_label"][0]
    assert bucket["samples"] == 4
    assert bucket["errors"] == 1


def test_new_fields_min_success_max_rt_merged():
    """success 累加、min_rt 取各 Agent 最小、max_rt 取最大（全局与 by_label）。"""
    s1 = {
        "samples": 3,
        "success": 2,
        "errors": 1,
        "min_rt": 50.0,
        "max_rt": 400.0,
        "p95_rt": 100.0,
        "max_tps": 2.0,
        "by_label": [
            {
                "label": "login",
                "sample_type": "request",
                "samples": 3,
                "success": 2,
                "errors": 1,
                "min_rt": 50.0,
                "max_rt": 400.0,
                "p95_rt": 100.0,
                "max_tps": 2.0,
            }
        ],
    }
    s2 = {
        "samples": 2,
        "success": 2,
        "errors": 0,
        "min_rt": 80.0,
        "max_rt": 200.0,
        "p95_rt": 300.0,
        "max_tps": 3.0,
        "by_label": [
            {
                "label": "login",
                "sample_type": "request",
                "samples": 2,
                "success": 2,
                "errors": 0,
                "min_rt": 80.0,
                "max_rt": 200.0,
                "p95_rt": 300.0,
                "max_tps": 3.0,
            }
        ],
    }
    merged = _merge_summaries([s1, s2])
    assert merged["success"] == 4
    assert merged["errors"] == 1
    assert merged["min_rt"] == 50.0
    assert merged["max_rt"] == 400.0
    bucket = merged["by_label"][0]
    assert bucket["success"] == 4
    assert bucket["min_rt"] == 50.0
    assert bucket["max_rt"] == 400.0


def test_legacy_summary_without_new_fields_fallback():
    """旧 Agent 缺 success/min_rt/max_rt：success 按 samples-errors 兜底，
    min_rt 无有效值保持 0（视为未上报），max_rt 取 max 不受缺省 0 影响。"""
    legacy = {
        "samples": 4,
        "errors": 1,
        "p95_rt": 10.0,
        "max_tps": 1.0,
        "by_label": [
            {"label": "ping", "samples": 4, "errors": 1, "p95_rt": 10.0, "max_tps": 1.0}
        ],
    }
    merged = _merge_summaries([legacy])
    assert merged["success"] == 3
    assert merged["min_rt"] == 0.0
    assert merged["max_rt"] == 0.0
    bucket = merged["by_label"][0]
    assert bucket["success"] == 3
    assert bucket["min_rt"] == 0.0
