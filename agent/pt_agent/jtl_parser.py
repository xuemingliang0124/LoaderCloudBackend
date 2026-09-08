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
- max_tps 用「单秒最大样本数」近似：按 timeStamp 向下取整到秒，统计每秒样本数取 max。
  JMeter CSV 的 timeStamp 是请求开始时间（毫秒），按它分桶即得 RPS 峰值。
- 增量 offset 记录绝对文件位置；JMeter 持续追加写入，每次只处理新增行。
  offset 落在行中间时丢弃不完整行（残行），避免解析错位。
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


def _parse_csv_sync(jtl_path: str) -> dict:
    """同步扫描 CSV JTL，返回原始聚合中间态。

    返回结构（内部用，executor 再拼装成协议字段）：
    {
      "samples": int, "errors": int,
      "elapsed": list[int],                # 全局延迟列表（算 p95）
      "by_label": {
        label: {"samples": int, "errors": int, "elapsed": list[int]}
      },
      "per_second": dict[int_second, int_count]   # 算 max_tps
    }
    """
    totals = {
        "samples": 0,
        "errors": 0,
        "elapsed": [],
        "by_label": defaultdict(lambda: {"samples": 0, "errors": 0, "elapsed": []}),
        "per_second": defaultdict(int),
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

            totals["samples"] += 1
            totals["elapsed"].append(elapsed)
            if not success:
                totals["errors"] += 1
            if ts > 0:
                totals["per_second"][ts // 1000] += 1

            bucket = totals["by_label"][label]
            bucket["samples"] += 1
            bucket["elapsed"].append(elapsed)
            if not success:
                bucket["errors"] += 1
    return totals


def _build_summary(parsed: dict, failed: bool) -> dict:
    """把中间态聚合成协议约定的 summary 结构。"""
    by_label = []
    for label, stats in parsed["by_label"].items():
        by_label.append(
            {
                "label": label,
                "samples": stats["samples"],
                "errors": stats["errors"],
                "p95_rt": _percentile_95(stats["elapsed"]),
                "max_tps": 0.0,  # label 级 max_tps 需独立 per_second，骨架暂不上报
            }
        )
    by_label.sort(key=lambda x: x["samples"], reverse=True)

    max_tps = 0.0
    if parsed["per_second"]:
        max_tps = float(max(parsed["per_second"].values()))

    return {
        "failed": failed,
        "samples": parsed["samples"],
        "errors": parsed["errors"],
        "p95_rt": _percentile_95(parsed["elapsed"]),
        "max_tps": max_tps,
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
            "errors": 0,
            "p95_rt": 0.0,
            "max_tps": 0.0,
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


def _parse_csv_increment_sync(jtl_path: str, last_offset: int) -> tuple[dict, int]:
    """同步增量解析，返回 (本批聚合结果, 新 offset)。

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

        result["samples"] += 1
        result["elapsed"].append(elapsed)
        result["threads"] = max(result["threads"], all_threads)
        if not success:
            result["errors"] += 1
        if ts > 0:
            result["per_second"][ts // 1000] += 1

        bucket = result["by_label"][label]
        bucket["samples"] += 1
        bucket["elapsed"].append(elapsed)
        bucket["threads"] = max(bucket["threads"], all_threads)
        if not success:
            bucket["errors"] += 1
        if ts > 0:
            bucket["per_second"][ts // 1000] += 1

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
    for label, stats in parsed["by_label"].items():
        lbl_samples = stats["samples"]
        lbl_elapsed = stats["elapsed"]
        by_label.append(
            {
                "label": label,
                "samples": lbl_samples,
                "interval_tps": lbl_samples / interval_s if interval_s > 0 else 0.0,
                "avg_rt": sum(lbl_elapsed) / len(lbl_elapsed) if lbl_elapsed else 0.0,
                "p95_rt": _percentile_95(lbl_elapsed),
                "err_rate": stats["errors"] / lbl_samples if lbl_samples else 0.0,
                "errors": stats["errors"],
                "threads": stats["threads"],
            }
        )
    by_label.sort(key=lambda x: x["samples"], reverse=True)

    return {
        "samples": samples,
        "interval_tps": interval_tps,
        "avg_rt": avg_rt,
        "p95_rt": p95_rt,
        "err_rate": err_rate,
        "errors": parsed["errors"],
        "threads": parsed["threads"],
        "by_label": by_label,
    }


async def parse_increment(
    jtl_path: str, last_offset: int = 0, interval_s: int = 5
) -> tuple[dict, int]:
    """异步增量解析入口，返回 (MSG_METRICS 协议字段, 新 offset)。

    供 _metrics_loop 周期调用：每次传上次返回的 new_offset，只处理新增行。
    返回的 metrics dict 不含 run_no，由调用方补。
    """
    if not Path(jtl_path).exists():
        return _build_increment_metrics(_empty_increment(), interval_s), last_offset
    parsed, new_offset = await asyncio.to_thread(
        _parse_csv_increment_sync, jtl_path, last_offset
    )
    return _build_increment_metrics(parsed, interval_s), new_offset
