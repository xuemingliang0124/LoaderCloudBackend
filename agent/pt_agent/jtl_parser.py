"""JTL 结果文件解析。

JMeter `-l xxx.jtl` 默认输出 CSV（首字符 `<` 时为 XML，仅做 CSV 支持）。
列固定头（JMeter 5.x 默认）：
  timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,
  success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect

本模块提供两类解析：
1. 终态汇总：执行结束后一次性扫描 JTL，产出 MSG_RESULT 的 summary 结构。
2. 增量解析：运行期按 offset tail JTL，产出 MSG_METRICS 的实时指标。

设计要点：
- 用 asyncio.to_thread 包装同步 csv 读取，避免阻塞事件循环。
- p95 用 numpy.percentile 默认 linear 插值口径（不外推越界）。
- avg_tps 用 JMeter 官方 throughput 口径：samples / 活跃墙钟时长，时长取
  末个请求结束时间（timeStamp+elapsed）与首个请求开始时间（timeStamp）之差，
  毫秒换算秒；单样本时时长即其自身 elapsed。
- avg_rt 为 elapsed 算术平均（毫秒），与运行期 MSG_METRICS 的 avg_rt 同口径。
- 增量 offset 记录绝对文件位置；JMeter 持续追加写入，每次只处理新增行。
  offset 落在行中间时丢弃不完整行（残行），避免解析错位。
- 事务/请求区分：JMeter 事务控制器生成的样本行在 responseMessage 携带官方标记
  "Number of samples in transaction : N, number of failing samples : M"
  （源码 TransactionSampler.setTransactionDone），失败事务同样携带该标记，
  据此做行级判定（与 label 无关，事务与取样器同名也不歧义）。
  全局口径（总 samples/TPS/p95/avg_rt）只计 request 行，事务行仅进入
  by_label（sample_type=transaction），避免事务父样本与子请求双计、p95 失真；
  整份 JTL 无 request 行的罕见配置（父样本模式关闭 subresults）下兜底回退
  为全量计入并告警。
- 指标字段：全局与 by_label 均输出 samples/success/errors（成功/失败笔数）与
  min_rt/max_rt/avg_rt/p95_rt（响应时间最小/最大/平均/95 分位）及 avg_tps
  （平均吞吐率）；success 由 samples-errors 推导，min/max 直接取自已保留的
  elapsed 列表（p95/avg 同源，无额外扫描开销）。
"""

import asyncio
import csv
from collections import defaultdict
from pathlib import Path

from loguru import logger


def _is_csv(jtl_path: str) -> bool:
    """首字符非 `<` 视为 CSV（XML 以 <?xml 起头）。空文件按 CSV 兜底。"""
    try:
        with open(jtl_path, "rb") as f:
            head = f.read(1)
    except FileNotFoundError:
        return False
    return head != b"<"


def _percentile_95(values: list[int]) -> float:
    """计算 p95（numpy.percentile 默认 linear 插值口径，不外推）。

    位置 = (n-1) * 0.95，落在两个样本之间时做线性插值。
    样本不足时返回 0.0；单样本返回该样本值。
    """
    if not values:
        return 0.0
    sv = sorted(values)
    if len(sv) == 1:
        return float(sv[0])
    pos = (len(sv) - 1) * 0.95
    lo = int(pos)
    hi = min(lo + 1, len(sv) - 1)
    frac = pos - lo
    return float(sv[lo] + (sv[hi] - sv[lo]) * frac)


# JMeter 事务行的官方标记（TransactionController 生成的事务样本收尾时写入，
# 成功/失败事务均携带，仅 N/M 计数不同）
_TRANSACTION_MARKER = "Number of samples in transaction"


def _is_transaction_row(row: dict) -> bool:
    """行级判定是否为事务控制器生成的样本。

    标记正常落在 responseMessage；responseCode 事务成功时为 OK、失败时复制
    首个子样本状态码，正常不含标记，两列都查属防御性兜底（列序/格式漂移）。
    """
    return _TRANSACTION_MARKER in (row.get("responseCode") or "") or (
        _TRANSACTION_MARKER in (row.get("responseMessage") or "")
    )


def _parse_csv_sync(jtl_path: str) -> dict:
    """同步扫描 CSV JTL，返回原始聚合中间态。

    返回结构（内部用，executor 再拼装成协议字段）：
    {
      "samples": int, "errors": int,     # 仅 request 行口径（全为事务时兜底回填）
      "elapsed": list[int],              # 全局延迟列表（算 avg/p95，仅 request 行）
      "first_ts": int|None,              # 首个请求开始毫秒（仅 request 行）
      "last_end": int,                   # 末个请求结束毫秒 ts+elapsed（仅 request 行）
      "by_label": {
        (label, sample_type): {"samples", "errors", "elapsed", "first_ts", "last_end"}
      }
    }
    """
    totals = {
        "samples": 0,
        "errors": 0,
        "elapsed": [],
        "first_ts": None,
        "last_end": 0,
        "by_label": defaultdict(
            lambda: {
                "samples": 0,
                "errors": 0,
                "elapsed": [],
                "first_ts": None,
                "last_end": 0,
            }
        ),
    }
    if not _is_csv(jtl_path):
        logger.warning(f"JTL 非格式或不存在，跳过解析: {jtl_path}")
        return totals

    with open(jtl_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        # 列缺失时 DictReader 返回 None，统一兜底
        for row in reader:
            try:
                ts = int(row.get("timeStamp") or 0)
                elapsed = int(row.get("elapsed") or 0)
            except ValueError:
                continue  # 跳过非法行（如 JMeter 启动期的占位行）
            label = row.get("label") or "_total"
            success = (row.get("success") or "").lower() == "true"
            stype = "transaction" if _is_transaction_row(row) else "request"

            bucket = totals["by_label"][(label, stype)]
            bucket["samples"] += 1
            bucket["elapsed"].append(elapsed)
            if not success:
                bucket["errors"] += 1
            if ts > 0:
                if bucket["first_ts"] is None or ts < bucket["first_ts"]:
                    bucket["first_ts"] = ts
                bucket["last_end"] = max(bucket["last_end"], ts + elapsed)

            # 全局口径只计 request 行，避免事务父样本与子请求双计
            if stype == "request":
                totals["samples"] += 1
                totals["elapsed"].append(elapsed)
                if not success:
                    totals["errors"] += 1
                if ts > 0:
                    if totals["first_ts"] is None or ts < totals["first_ts"]:
                        totals["first_ts"] = ts
                    totals["last_end"] = max(totals["last_end"], ts + elapsed)

    # 兜底：整份 JTL 无 request 行（如父样本模式关闭 subresults）时，
    # 全局口径回退为全量计入并告警，避免汇总归零
    if totals["samples"] == 0 and totals["by_label"]:
        logger.warning("JTL 无 request 行，全局汇总回退为含事务行")
        for (_label, stype), bucket in totals["by_label"].items():
            if stype != "transaction":
                continue
            totals["samples"] += bucket["samples"]
            totals["elapsed"].extend(bucket["elapsed"])
            totals["errors"] += bucket["errors"]
            if bucket["first_ts"] is not None:
                if totals["first_ts"] is None or bucket["first_ts"] < totals["first_ts"]:
                    totals["first_ts"] = bucket["first_ts"]
                totals["last_end"] = max(totals["last_end"], bucket["last_end"])
    return totals


def _avg_tps(samples: int, first_ts: int | None, last_end: int) -> float:
    """JMeter throughput 口径：samples / 活跃墙钟时长（秒）。

    时长 = 末请求结束(ts+elapsed) − 首请求开始(ts)，毫秒换算秒。
    无有效时间戳（first_ts is None）或时长非正（elapsed 全 0）时返回 0.0。
    """
    if first_ts is None:
        return 0.0
    duration_ms = last_end - first_ts
    if duration_ms <= 0:
        return 0.0
    return samples * 1000.0 / duration_ms


def _build_summary(parsed: dict, failed: bool) -> dict:
    """把中间态聚合成协议约定的 summary 结构。"""
    by_label = []
    for (label, stype), stats in parsed["by_label"].items():
        lbl_elapsed = stats["elapsed"]
        by_label.append(
            {
                "label": label,
                "sample_type": stype,
                "samples": stats["samples"],
                "success": stats["samples"] - stats["errors"],
                "errors": stats["errors"],
                "min_rt": float(min(lbl_elapsed)) if lbl_elapsed else 0.0,
                "max_rt": float(max(lbl_elapsed)) if lbl_elapsed else 0.0,
                "avg_rt": (
                    sum(lbl_elapsed) / len(lbl_elapsed) if lbl_elapsed else 0.0
                ),
                "p95_rt": _percentile_95(lbl_elapsed),
                "avg_tps": _avg_tps(
                    stats["samples"], stats["first_ts"], stats["last_end"]
                ),
            }
        )
    by_label.sort(key=lambda x: x["samples"], reverse=True)

    elapsed = parsed["elapsed"]
    return {
        "failed": failed,
        "samples": parsed["samples"],
        "success": parsed["samples"] - parsed["errors"],
        "errors": parsed["errors"],
        "min_rt": float(min(elapsed)) if elapsed else 0.0,
        "max_rt": float(max(elapsed)) if elapsed else 0.0,
        "avg_rt": sum(elapsed) / len(elapsed) if elapsed else 0.0,
        "p95_rt": _percentile_95(elapsed),
        "avg_tps": _avg_tps(parsed["samples"], parsed["first_ts"], parsed["last_end"]),
        "by_label": by_label,
    }


async def parse_summary(jtl_path: str, failed: bool = False) -> dict:
    """异步入口：扫描 JTL 生成协议约定的 summary。

    Args:
        jtl_path: JTL 文件绝对路径。
        failed: 该 run 是否视为失败（停止/异常场景）。仅写入 summary.failed 字段，
            不影响指标计算——即便停止也要把已采集的样本汇总上报。

    Returns:
        summary dict，字段与 protocol.py MSG_RESULT 注释一致。
    """
    if not Path(jtl_path).exists():
        logger.warning(f"JTL 不存在，返回空 summary: {jtl_path}")
        return {
            "failed": True,
            "samples": 0,
            "success": 0,
            "errors": 0,
            "min_rt": 0.0,
            "max_rt": 0.0,
            "avg_rt": 0.0,
            "p95_rt": 0.0,
            "avg_tps": 0.0,
            "by_label": [],
        }
    parsed = await asyncio.to_thread(_parse_csv_sync, jtl_path)
    return _build_summary(parsed, failed=failed)


def _empty_increment() -> dict:
    """增量解析的空结果结构。比终态多 threads 字段（allThreads 列）。"""
    return {
        "samples": 0,
        "errors": 0,
        "elapsed": [],
        "threads": 0,
        "per_second": defaultdict(int),
        "by_label": defaultdict(
            lambda: {
                "samples": 0,
                "errors": 0,
                "elapsed": [],
                "threads": 0,
                "per_second": defaultdict(int),
            }
        ),
    }


def _parse_csv_increment_sync(
    jtl_path: str, last_offset: int, state: dict | None = None
) -> tuple[dict, int]:
    """同步增量解析，返回 (本批聚合结果, 新 offset)。

    state：调用方跨批持有的可变 dict，记忆「本次 run 是否出现过 request 行」，
    用于全事务配置下的全局口径兜底；传 None 时按单批独立处理。

    offset 语义：绝对文件位置。首次（last_offset=0 或 < header_end）从 header 后开始；
    后续从 last_offset 起，跳过残行（offset 落在行中间时丢弃不完整行）。
    JMeter 持续追加写入，本函数只处理自上次以来的新增行。
    """
    result = _empty_increment()
    if not _is_csv(jtl_path):
        return result, last_offset

    with open(jtl_path, "rb") as f:
        # 读 header（每次读，开销可忽略；列顺序随 JMeter 版本可能变）
        header_line = f.readline()
        if not header_line:
            return result, last_offset
        try:
            header = next(csv.reader([header_line.decode("utf-8", errors="replace")]))
        except StopIteration:
            return result, last_offset
        header_end = f.tell()

        # 定位到上次读完的位置（不低于 header_end）。
        # new_offset 记录的是 f.read() 后的 tell()，必指向完整行的下一行行首，
        # 故无需 readline 跳残行；若 offset 异常落在行中间，CSV 解析会因列数
        # 不对抛 ValueError 被 except 跳过，不会错位。
        start = max(last_offset, header_end)
        f.seek(start)
        chunk = f.read()
        new_offset = f.tell()

    if not chunk:
        return result, last_offset

    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = dict(zip(header, next(csv.reader([line]))))
        except (StopIteration, ValueError):
            continue
        try:
            ts = int(row.get("timeStamp") or 0)
            elapsed = int(row.get("elapsed") or 0)
            all_threads = int(row.get("allThreads") or 0)
        except ValueError:
            continue
        label = row.get("label") or "_total"
        success = (row.get("success") or "").lower() == "true"
        stype = "transaction" if _is_transaction_row(row) else "request"

        bucket = result["by_label"][(label, stype)]
        bucket["samples"] += 1
        bucket["elapsed"].append(elapsed)
        bucket["threads"] = max(bucket["threads"], all_threads)
        if not success:
            bucket["errors"] += 1
        if ts > 0:
            bucket["per_second"][ts // 1000] += 1

        # 全局口径只计 request 行，避免事务父样本与子请求双计
        if stype == "request":
            result["samples"] += 1
            result["elapsed"].append(elapsed)
            result["threads"] = max(result["threads"], all_threads)
            if not success:
                result["errors"] += 1
            if ts > 0:
                result["per_second"][ts // 1000] += 1

    # 运行期兜底：本次 run 从未出现过 request 行（如父样本模式关闭 subresults）
    # 且本批只有事务行时，全局口径回退为全量计入并告警，避免实时曲线全程为零。
    # 混合场景下某一批可能只含事务行（父行落在批边界之后），此时不上报全局，
    # 防止与前一批已计入的子请求行双计。
    if state is None:
        state = {}
    if result["samples"] > 0:
        state["saw_request"] = True
    elif any(key[1] == "transaction" for key in result["by_label"]):
        if not state.get("saw_request"):
            logger.warning("JTL 增量无 request 行，全局口径回退为含事务行")
            for (_label, stype), bucket in result["by_label"].items():
                if stype != "transaction":
                    continue
                result["samples"] += bucket["samples"]
                result["elapsed"].extend(bucket["elapsed"])
                result["threads"] = max(result["threads"], bucket["threads"])
                result["errors"] += bucket["errors"]
                for sec, cnt in bucket["per_second"].items():
                    result["per_second"][sec] += cnt

    return result, new_offset


def _build_increment_metrics(parsed: dict, interval_s: int) -> dict:
    """把增量中间态聚合成 MSG_METRICS 协议字段。run_no 由调用方补。"""
    samples = parsed["samples"]
    elapsed = parsed["elapsed"]
    interval_tps = samples / interval_s if interval_s > 0 else 0.0
    avg_rt = sum(elapsed) / len(elapsed) if elapsed else 0.0
    p95_rt = _percentile_95(elapsed)
    err_rate = parsed["errors"] / samples if samples else 0.0

    by_label = []
    for (label, stype), stats in parsed["by_label"].items():
        lbl_samples = stats["samples"]
        lbl_elapsed = stats["elapsed"]
        by_label.append(
            {
                "label": label,
                "sample_type": stype,
                "samples": lbl_samples,
                "success": lbl_samples - stats["errors"],
                "interval_tps": lbl_samples / interval_s if interval_s > 0 else 0.0,
                "avg_rt": sum(lbl_elapsed) / len(lbl_elapsed) if lbl_elapsed else 0.0,
                "min_rt": float(min(lbl_elapsed)) if lbl_elapsed else 0.0,
                "max_rt": float(max(lbl_elapsed)) if lbl_elapsed else 0.0,
                "p95_rt": _percentile_95(lbl_elapsed),
                "err_rate": stats["errors"] / lbl_samples if lbl_samples else 0.0,
                "errors": stats["errors"],
                "threads": stats["threads"],
            }
        )
    by_label.sort(key=lambda x: x["samples"], reverse=True)

    return {
        "samples": samples,
        "success": samples - parsed["errors"],
        "interval_tps": interval_tps,
        "avg_rt": avg_rt,
        "min_rt": float(min(elapsed)) if elapsed else 0.0,
        "max_rt": float(max(elapsed)) if elapsed else 0.0,
        "p95_rt": p95_rt,
        "err_rate": err_rate,
        "errors": parsed["errors"],
        "threads": parsed["threads"],
        "by_label": by_label,
    }


async def parse_increment(
    jtl_path: str, last_offset: int = 0, interval_s: int = 5, state: dict | None = None
) -> tuple[dict, int]:
    """异步增量解析入口，返回 (MSG_METRICS 协议字段, 新 offset)。

    供 _metrics_loop 周期调用：每次传上次返回的 new_offset，只处理新增行。
    state 为调用方跨批持有的可变 dict（记忆是否出现过 request 行，供全事务
    配置下的全局口径兜底），传 None 时按单批独立处理。
    返回的 metrics dict 不含 run_no，由调用方补。
    """
    if not Path(jtl_path).exists():
        return _build_increment_metrics(_empty_increment(), interval_s), last_offset
    parsed, new_offset = await asyncio.to_thread(
        _parse_csv_increment_sync, jtl_path, last_offset, state
    )
    return _build_increment_metrics(parsed, interval_s), new_offset
