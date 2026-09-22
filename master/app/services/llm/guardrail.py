"""FR-08 指标校验与一致性检查（SRS FR-08 + 附录 A，权重最高 6 分）。

职责：
- `extract_metric_values`：从 AnswerOutput.answer 自然语言中正则抽取
  tps / p95_ms / error_rate 数值；error_rate 归一化为百分数（0-100），
  有百分号按百分数、无百分号且 ≤1 按比率（0~1）×100
- `apply_metric_guardrail`：纯函数核心（不触 ES，可离线单测）——抽取值与
  ES 基准逐字段比对，相对误差 |llm-actual|/actual > metric_tolerance(±5%)
  视为不一致：
  - 不一致 → notes 追加 "Mismatch with actual metrics: {field}"，
    confidence 压至 ≤ 0.7（SRS FR-08 强约束）
  - 全部一致 → notes 追加 "Metrics verified"
  - run_no 无基准数据 → 跳过校验，notes 标注 "无法获取基准指标"
  - 指标关键词后是"未知/N/A/暂无"等占位 → 该字段标记 N/A 并写日志
- `validate_metrics`：异步入口（dict→dict，对齐 SRS 函数级约定
  `(run_no, payload) -> payload`），经 es_client.get_run_metrics 取基准；
  ES 异常不外抛（降级为无基准，NFR-02 鲁棒性）
- `build_metric_guardrail`：RunnableLambda 封装（SRS 2.3 能力映射：
  FR-08 为 RunnableLambda 自定义校验逻辑），可挂 LCEL 链
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableLambda
from loguru import logger

from app.core.config import get_settings
from app.services import es_client
from app.services.llm.client import AnswerOutput

# 支持的指标字段（SRS FR-08：tps, p95_ms, error_rate）
METRIC_FIELDS: tuple[str, ...] = ("tps", "p95_ms", "error_rate")

MISMATCH_NOTE = "Mismatch with actual metrics"
VERIFIED_NOTE = "Metrics verified"
NO_BASELINE_NOTE = "无法获取基准指标"
# SRS FR-08：指标超差时 confidence 降至 ≤ 0.7
MISMATCH_CONFIDENCE_CAP = 0.7

_NUMBER = r"\d+(?:\.\d+)?"
# 关键词与数值之间允许的间隔：CJK/标点/空格 ≤10 字符，禁止拉丁字母与数字
# （避免 "TPS 未知，P95 为 120" 跨行误捕 120 作为 TPS）
_GAP = r"[^0-9A-Za-z%]{0,10}?"

_RE_TPS_FIRST = re.compile(rf"tps{_GAP}({_NUMBER})", re.IGNORECASE)
_RE_TPS_THROUGHPUT = re.compile(rf"吞吐[量率]?{_GAP}({_NUMBER})")
# 数值前置形式："压到 500 TPS" / "480 笔/秒"（关键词后置，兜底）
_RE_TPS_BEFORE = re.compile(rf"({_NUMBER})\s*(?:tps|笔/秒|笔每秒)", re.IGNORECASE)
_RE_P95 = re.compile(
    rf"p95(?:[_\s]?(?:rt|ms|响应时间|延迟|分位线|分位数|分位))?{_GAP}({_NUMBER})",
    re.IGNORECASE,
)
_RE_ERROR_RATE = re.compile(
    rf"(?:错误率|error[_\s]?rate|err[_\s]?rate){_GAP}({_NUMBER})\s*(%|％)?",
    re.IGNORECASE,
)

# 关键词出现判定（用于"数值无法解析"检测）
_RE_KEYWORD = {
    "tps": re.compile(r"tps|吞吐[量率]?|笔/秒|笔每秒", re.IGNORECASE),
    "p95_ms": re.compile(r"p\s*95", re.IGNORECASE),
    "error_rate": re.compile(r"错误率|error[_\s]?rate|err[_\s]?rate", re.IGNORECASE),
}
# 显式占位符（SRS：数值无法解析 → N/A 并写日志）
_RE_UNPARSEABLE = re.compile(
    r"未知|n/?a|暂无|缺失|无法获取|unknown|无数据", re.IGNORECASE
)

# "P95（95分位）约 120ms" 中的解释性括号会让数值抽取误捕 95，先剥离
_RE_P95_PAREN = re.compile(
    r"[（(]\s*95\s*(?:分位(?:数|线)?|th|percentile)[^）)]*[）)]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractedMetric:
    """从回答中抽取出的单个指标值。"""

    field: str
    value: float  # 归一化值：tps/p95 原值；error_rate 为百分数（0-100）
    raw: str  # 原始数值文本
    percent: bool  # error_rate 是否以百分号给出


def _to_error_rate_percent(number: float, has_percent: bool) -> float:
    """错误率口径归一化到百分数（0-100）。

    - 带 %：值即百分数（0.5% → 0.5）
    - 无 % 且值 ≤ 1：按比率（0~1）×100（0.005 → 0.5）
    - 无 % 且值 > 1：按百分数（"错误率 1.2" 视作 1.2%）
    """
    if has_percent:
        return number
    if number <= 1:
        return number * 100
    return number


def extract_metric_values(answer: str) -> dict[str, ExtractedMetric]:
    """从自然语言回答中抽取 tps/p95_ms/error_rate 数值（未出现的字段不返回）。"""
    text = _RE_P95_PAREN.sub("", answer or "")
    found: dict[str, ExtractedMetric] = {}

    m = _RE_TPS_FIRST.search(text) or _RE_TPS_THROUGHPUT.search(text)
    if m is None:
        m = _RE_TPS_BEFORE.search(text)
    if m is not None:
        found["tps"] = ExtractedMetric(
            field="tps", value=float(m.group(1)), raw=m.group(1), percent=False
        )

    m = _RE_P95.search(text)
    if m is not None:
        found["p95_ms"] = ExtractedMetric(
            field="p95_ms", value=float(m.group(1)), raw=m.group(1), percent=False
        )

    m = _RE_ERROR_RATE.search(text)
    if m is not None:
        has_percent = bool(m.group(2))
        found["error_rate"] = ExtractedMetric(
            field="error_rate",
            value=_to_error_rate_percent(float(m.group(1)), has_percent),
            raw=m.group(1),
            percent=has_percent,
        )

    return found


def _find_unparseable_fields(
    answer: str, extracted: dict[str, ExtractedMetric]
) -> list[str]:
    """关键词出现但数值无法解析（且其后是显式占位符）的字段 → 标记 N/A。

    仅在关键词后 20 字符窗口内出现"未知/N/A/暂无/缺失"等占位词时判定，
    避免把 "tps/p95_ms/error_rate 三个指标" 这类字段名罗列误报为 N/A。
    """
    text = answer or ""
    unparseable: list[str] = []
    for field in METRIC_FIELDS:
        if field in extracted:
            continue
        km = _RE_KEYWORD[field].search(text)
        if km is None:
            continue
        window = text[km.start() : km.start() + 20]
        if _RE_UNPARSEABLE.search(window):
            unparseable.append(field)
    return unparseable


def _within_tolerance(llm_value: float, actual: float, tolerance: float) -> bool:
    """±5% 容差判定（边界 ≤tolerance 视为一致）；基准为 0 时仅零值一致。"""
    if actual == 0:
        return llm_value == 0
    return abs(llm_value - actual) / abs(actual) <= tolerance


def _append_note(existing: str, note: str) -> str:
    """notes 追加（分号拼接，去重去空）。"""
    return "; ".join(x for x in (existing, note) if x)


def apply_metric_guardrail(
    payload: dict[str, Any],
    actual: dict[str, Any] | None,
    *,
    run_no: str | int = "",
    tolerance: float = 0.05,
) -> dict[str, Any]:
    """FR-08 纯函数核心：payload（AnswerOutput 五字段 dict）→ 校验后的新 payload。

    - actual 为 None（run_no 不存在/ES 无数据/ES 异常）→ 跳过校验，
      仅当回答确实提到指标时在 notes 标注"无法获取基准指标"
    - 抽取值与 actual 逐字段比对：超差写 mismatch notes 并压 confidence，
      全部一致写 "Metrics verified"
    - 追加 run_summary:{run_no} 引用（附录 A citations 范式）
    - used_metrics 缺省时按实际抽取字段回填（FR-09 字段补齐）
    """
    out = dict(payload)
    answer = str(out.get("answer") or "")
    notes = str(out.get("notes") or "")
    try:
        confidence = float(out.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    citations = list(out.get("citations") or [])
    used_metrics = list(out.get("used_metrics") or [])

    extracted = extract_metric_values(answer)
    unparseable = _find_unparseable_fields(answer, extracted)
    for field in unparseable:
        logger.warning(f"FR-08 指标数值无法解析，标记 N/A: run={run_no} field={field}")
        notes = _append_note(notes, f"指标 {field} 数值无法解析，标记 N/A")

    mentions_metrics = bool(extracted) or bool(unparseable)

    if actual is None:
        if mentions_metrics:
            logger.info(f"FR-08 无基准指标，跳过校验: run={run_no}")
            notes = _append_note(
                notes, f"{NO_BASELINE_NOTE}（run_no={run_no}），跳过指标校验"
            )
        out["notes"] = notes
        return out

    mismatched: list[str] = []
    matched: list[str] = []
    for field in METRIC_FIELDS:
        item = extracted.get(field)
        if item is None:
            continue
        actual_value = actual.get(field)
        if actual_value is None:
            logger.warning(f"FR-08 基准缺少字段，标记 N/A: run={run_no} field={field}")
            notes = _append_note(notes, f"指标 {field} 基准缺失，标记 N/A")
            continue
        if _within_tolerance(item.value, float(actual_value), tolerance):
            matched.append(field)
        else:
            mismatched.append(field)
            logger.info(
                f"FR-08 指标超差: run={run_no} field={field} "
                f"llm={item.value} actual={actual_value}"
            )

    for field in mismatched:
        notes = _append_note(notes, f"{MISMATCH_NOTE}: {field}")
    if matched and not mismatched:
        notes = _append_note(notes, VERIFIED_NOTE)
    if mismatched:
        confidence = min(confidence, MISMATCH_CONFIDENCE_CAP)

    # 比对实际发生过（含一致/超差）→ 回填 used_metrics + run_summary 引用
    if matched or mismatched:
        for field in METRIC_FIELDS:
            if field in extracted and field not in used_metrics:
                used_metrics.append(field)
        if run_no:
            citation = f"run_summary:{run_no}"
            if citation not in citations:
                citations.append(citation)

    out["notes"] = notes
    out["confidence"] = confidence
    out["citations"] = citations
    out["used_metrics"] = used_metrics or None
    return out


async def validate_metrics(
    run_no: str | int,
    payload: dict[str, Any],
    tolerance: float | None = None,
) -> dict[str, Any]:
    """FR-08 异步入口：取 ES 基准 → apply_metric_guardrail（SRS: (run_no,payload)->payload）。

    ES 查询异常不外抛（NFR-02：降级为"无基准跳过校验"，主问答链路不中断）。
    """
    if tolerance is None:
        tolerance = get_settings().metric_tolerance
    try:
        actual = await es_client.get_run_metrics(str(run_no))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"FR-08 基准指标查询失败，按无基准跳过: run={run_no} "
            f"{type(exc).__name__}: {exc}"
        )
        actual = None
    return apply_metric_guardrail(
        payload, actual, run_no=str(run_no), tolerance=tolerance
    )


async def validate_answer(
    run_no: str | int,
    answer: AnswerOutput,
    tolerance: float | None = None,
) -> AnswerOutput:
    """AnswerOutput 适配（orchestrator 消费）：dict 校验后重建模型。"""
    payload = await validate_metrics(run_no, answer.model_dump(), tolerance)
    return AnswerOutput(**payload)


def build_metric_guardrail(
    run_no: str | int, tolerance: float | None = None
) -> RunnableLambda:
    """FR-08 的 LangChain Runnable 封装（SRS 2.3：RunnableLambda 自定义校验）。

    输入/输出均为 AnswerOutput 的 dict 形态，可直接串入 LCEL：
    `chain | guard | parser`；ainvoke 走异步 ES 查询。
    """

    async def _invoke(payload: dict[str, Any]) -> dict[str, Any]:
        return await validate_metrics(run_no, payload, tolerance)

    return RunnableLambda(_invoke)
