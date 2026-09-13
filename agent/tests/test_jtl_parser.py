"""JTL 解析单测：事务/请求行级区分、全局口径与无 request 行兜底。

事务行判定依据 JMeter 官方标记（responseMessage 携带
"Number of samples in transaction : N, number of failing samples : M"），
与 label 无关——事务与取样器同名也不歧义。
"""

import asyncio
from pathlib import Path

from pt_agent.jtl_parser import (
    _parse_csv_increment_sync,
    _parse_csv_sync,
    parse_increment,
    parse_summary,
)

_HEADER = (
    "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,"
    "success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,"
    "IdleTime,Connect"
)

_TX_MSG_OK = "Number of samples in transaction : 2, number of failing samples : 0"
_TX_MSG_FAIL = "Number of samples in transaction : 2, number of failing samples : 1"


def _write_jtl(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")


def _req_row(label: str, ts: int, elapsed: int, success: str = "true") -> str:
    return (
        f"{ts},{elapsed},{label},200,OK,thread-1,text,{success},,"
        f"1024,100,1,1,http://x/a,10,0,5"
    )


def _tx_row(label: str, ts: int, elapsed: int, success: str = "true") -> str:
    """事务行：responseCode 成功时为 OK、失败时复制首个子样本状态码。"""
    code = "OK" if success == "true" else "500"
    msg = _TX_MSG_OK if success == "true" else _TX_MSG_FAIL
    return (
        f'{ts},{elapsed},{label},{code},"{msg}",thread-1,,{success},,'
        f"1124,100,1,1,,0,0,5"
    )


# ---------- 终态汇总 ----------


def test_summary_same_label_request_and_transaction_separated(tmp_path: Path):
    """事务与取样器同名：行级判定不歧义，全局口径只计请求行。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(
        jtl,
        [
            _req_row("下单", 1694000000000, 100),
            _req_row("下单", 1694000000300, 200),
            _tx_row("下单", 1694000000000, 300),
        ],
    )
    parsed = _parse_csv_sync(str(jtl))
    assert parsed["samples"] == 2  # 事务行不计入全局
    assert set(parsed["by_label"].keys()) == {
        ("下单", "request"),
        ("下单", "transaction"),
    }
    assert parsed["by_label"][("下单", "request")]["samples"] == 2
    assert parsed["by_label"][("下单", "transaction")]["samples"] == 1

    summary = asyncio.run(parse_summary(str(jtl)))
    assert summary["samples"] == 2
    by_key = {(x["label"], x["sample_type"]): x for x in summary["by_label"]}
    assert set(by_key.keys()) == {("下单", "request"), ("下单", "transaction")}
    # label 级 max_tps 由 per_second 真实计算（此前骨架恒 0）
    assert by_key[("下单", "request")]["max_tps"] >= 1.0


def test_summary_failed_transaction_classified_and_bucketed(tmp_path: Path):
    """失败事务仍携带官方标记，归入事务桶且错误计入桶内而非全局。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(
        jtl,
        [
            _req_row("下单", 1694000000000, 100),
            _tx_row("下单", 1694000000000, 300, success="false"),
        ],
    )
    parsed = _parse_csv_sync(str(jtl))
    assert parsed["samples"] == 1
    assert parsed["errors"] == 0  # 全局只计 request 行
    assert parsed["by_label"][("下单", "transaction")]["errors"] == 1

    summary = asyncio.run(parse_summary(str(jtl)))
    tx = next(x for x in summary["by_label"] if x["sample_type"] == "transaction")
    assert tx["errors"] == 1


def test_summary_fallback_when_no_request_rows(tmp_path: Path):
    """整份 JTL 无 request 行（父样本模式关闭 subresults）→ 全局回退为全量。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(
        jtl,
        [
            _tx_row("T1", 1694000000000, 300),
            _tx_row("T2", 1694000000400, 200),
        ],
    )
    summary = asyncio.run(parse_summary(str(jtl)))
    assert summary["samples"] == 2
    assert all(x["sample_type"] == "transaction" for x in summary["by_label"])


# ---------- 增量解析 ----------


def test_increment_mixed_batches_no_double_count(tmp_path: Path):
    """混合场景：事务行与子请求分属两批时，全局不双计；by_label 保留事务条目。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(jtl, [_req_row("下单", 1694000000000, 100)])
    state: dict = {}
    _, offset = _parse_csv_increment_sync(str(jtl), 0, state)
    assert state["saw_request"] is True

    with open(jtl, "a", encoding="utf-8") as f:
        f.write(_tx_row("下单", 1694000000500, 300) + "\n")

    parsed, _ = _parse_csv_increment_sync(str(jtl), offset, state)
    assert parsed["samples"] == 0  # 本批只有事务行且 run 已见 request → 全局零值
    assert parsed["by_label"][("下单", "transaction")]["samples"] == 1


def test_increment_fallback_all_transactions(tmp_path: Path):
    """全事务配置（从未出现 request 行）：逐批兜底计入全局，实时曲线不归零。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(jtl, [_tx_row("T1", 1694000000000, 300)])
    state: dict = {}
    metrics, offset = _parse_csv_increment_sync(str(jtl), 0, state)
    assert metrics["samples"] == 1

    with open(jtl, "a", encoding="utf-8") as f:
        f.write(_tx_row("T1", 1694000000500, 200) + "\n")
    metrics, _ = _parse_csv_increment_sync(str(jtl), offset, state)
    assert metrics["samples"] == 1


def test_parse_increment_async_entry_carries_sample_type(tmp_path: Path):
    """异步入口透传 state，by_label 元素带 sample_type。"""
    jtl = tmp_path / "a.jtl"
    _write_jtl(
        jtl,
        [
            _req_row("下单", 1694000000000, 100),
            _tx_row("下单", 1694000000100, 300),
        ],
    )
    state: dict = {}
    metrics, _ = asyncio.run(parse_increment(str(jtl), 0, 5, state))
    assert metrics["samples"] == 1
    by_key = {(x["label"], x["sample_type"]) for x in metrics["by_label"]}
    assert ("下单", "request") in by_key
    assert ("下单", "transaction") in by_key
    assert state["saw_request"] is True
