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
