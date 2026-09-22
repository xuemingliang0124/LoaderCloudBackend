"""L4 压测报告生成（Stage 5，SRS FR-10 + 报告模板草案）。

职责：
- `generate_report(run_no, db)`：端到端报告生成入口
  1. 从 DB 取 ScenarioRun + Scenario（场景配置：名称/类型/duration/agents）
  2. 从 ES 取基准指标（es_client.get_run_metrics → tps/p95/error_rate/samples）
     与实时汇总（query_realtime_summary → by_label 明细）
  3. 构造 Prompt（plan 第五节模板），调 LLM 生成 Markdown
  4. 写 MinIO（`reports/{run_no}/llm-report.md`），返回 ReportGenerateOut
- LLM 未配置 → 用模板降级生成（FakeListChatModel 走 FALLBACK_RESPONSE，
  此时用数据模板拼 Markdown 而非 LLM 输出，保证报告仍可用）
- ES/MinIO 异常不外抛：报告内标注"数据不可用"，confidence 降至 0.5

报告章节（plan 第 191-195 行）：
1. 执行概览（run_no、场景、时间、Agent 数、成功/失败数）
2. 性能指标（TPS、P95、错误率，与 SLA 对比）
3. 资源使用（Agent CPU/内存峰值——ES 无此数据时标"数据不可用"）
4. 结论与建议（是否达标、瓶颈分析、优化建议）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services import es_client, storage
from app.services.llm.client import get_llm

REPORT_PROMPT_TEMPLATE = """你是性能测试报告生成助手。基于以下压测数据生成 Markdown 格式的测试报告。

## 输入数据
- 运行编号：{run_no}
- 场景名称：{scenario_name}
- 场景类型：{scenario_type}
- 执行状态：{run_status}
- Agent 数量：{agent_count}
- 预期结果数：{expected_results}
- 执行汇总（ES pt-summary / 实时聚合）：{summary_json}
- 指标基准（归一化）：{metrics_json}
- 场景配置：{scenario_config}

## 输出要求
生成一份结构化 Markdown 报告，包含以下章节：
1. 执行概览（运行编号、场景、时间、Agent 数、成功/失败数）
2. 性能指标（TPS、P95、错误率，与 SLA 对比）
3. 资源使用（Agent CPU/内存峰值）
4. 结论与建议（是否达标、瓶颈分析、优化建议）

## 约束
- 仅基于提供的数据，禁止编造数值
- 数值保留 2 位小数
- 如数据缺失，标注"数据不可用"而非留空
- 引用格式：[run_summary:{run_no}] / [metrics:{run_no}:{{field}}]
"""

# 降级模板（LLM 不可用时直接拼装，保证报告仍可交付）
_FALLBACK_TEMPLATE = """# 压测报告：{run_no}

## 1. 执行概览

| 项目 | 值 |
| --- | --- |
| 运行编号 | {run_no} |
| 场景名称 | {scenario_name} |
| 场景类型 | {scenario_type} |
| 执行状态 | {run_status} |
| Agent 数量 | {agent_count} |
| 预期结果数 | {expected_results} |
| 样本总数 | {samples} |
| 成功数 | {success} |
| 失败数 | {errors} |

## 2. 性能指标

| 指标 | 值 |
| --- | --- |
| TPS | {tps} |
| P95 (ms) | {p95_ms} |
| 错误率 (%) | {error_rate} |

> 数据来源：{metrics_source}

## 3. 资源使用

Agent CPU/内存峰值数据不可用（ES 未采集资源指标）。

## 4. 结论与建议

{conclusion}

> 本报告由降级模板生成（LLM 未配置或调用失败），数据均来自 ES 基准指标。
> 引用：[run_summary:{run_no}]
"""


@dataclass
class ReportContext:
    """报告生成所需的上下文数据（从 DB + ES 聚合）。"""

    run_no: str
    scenario_name: str = "数据不可用"
    scenario_type: str = "数据不可用"
    run_status: str = "数据不可用"
    agent_count: int = 0
    expected_results: int = 0
    samples: int = 0
    success: int = 0
    errors: int = 0
    tps: float = 0.0
    p95_ms: float = 0.0
    error_rate: float = 0.0
    metrics_source: str = "数据不可用"
    summary_json: str = "{}"
    metrics_json: str = "{}"
    scenario_config: str = "{}"
    raw_summary: dict[str, Any] = field(default_factory=dict)
    raw_metrics: dict[str, Any] = field(default_factory=dict)


async def _gather_context(run_no: str, db: AsyncSession) -> ReportContext:
    """从 DB + ES 聚合报告上下文（各步骤独立容错）。"""
    ctx = ReportContext(run_no=run_no)

    # DB: ScenarioRun + Scenario
    try:
        run = (
            await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no))
        ).scalar_one_or_none()
        if run:
            ctx.run_status = (
                run.status.value if hasattr(run.status, "value") else str(run.status)
            )
            ctx.agent_count = len(run.agent_ids or [])
            ctx.expected_results = run.expected_results or 0
            scenario = (
                await db.execute(select(Scenario).where(Scenario.id == run.scenario_id))
            ).scalar_one_or_none()
            if scenario:
                ctx.scenario_name = scenario.name
                ctx.scenario_type = (
                    scenario.scenario_type.value
                    if hasattr(scenario.scenario_type, "value")
                    else str(scenario.scenario_type)
                )
                ctx.scenario_config = json.dumps(
                    {
                        "duration": scenario.duration,
                        "param_overrides": scenario.param_overrides or {},
                        "description": scenario.description,
                    },
                    ensure_ascii=False,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"报告上下文 DB 查询失败: {type(exc).__name__}: {exc}")

    # ES: 基准指标
    try:
        metrics = await es_client.get_run_metrics(run_no)
        if metrics:
            ctx.raw_metrics = metrics
            ctx.tps = round(float(metrics.get("tps") or 0), 2)
            ctx.p95_ms = round(float(metrics.get("p95_ms") or 0), 2)
            ctx.error_rate = round(float(metrics.get("error_rate") or 0), 2)
            ctx.samples = int(metrics.get("samples") or 0)
            ctx.metrics_source = metrics.get("source", "数据不可用")
            ctx.metrics_json = json.dumps(metrics, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"报告上下文 ES 基准指标查询失败: {type(exc).__name__}: {exc}")

    # ES: 汇总明细（summary 或 realtime）
    try:
        summary = await es_client.query_summary(run_no)
        if summary is None:
            summary = await es_client.query_realtime_summary(run_no)
        if summary:
            ctx.raw_summary = summary
            ctx.samples = int(summary.get("samples") or ctx.samples)
            ctx.success = int(summary.get("success") or 0)
            ctx.errors = int(summary.get("errors") or 0)
            ctx.summary_json = json.dumps(summary, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"报告上下文 ES 汇总查询失败: {type(exc).__name__}: {exc}")

    return ctx


def _build_prompt(ctx: ReportContext) -> str:
    """构造 LLM 报告生成 Prompt（plan 第五节模板）。"""
    return REPORT_PROMPT_TEMPLATE.format(
        run_no=ctx.run_no,
        scenario_name=ctx.scenario_name,
        scenario_type=ctx.scenario_type,
        run_status=ctx.run_status,
        agent_count=ctx.agent_count,
        expected_results=ctx.expected_results,
        summary_json=ctx.summary_json,
        metrics_json=ctx.metrics_json,
        scenario_config=ctx.scenario_config,
    )


def _build_fallback_markdown(ctx: ReportContext) -> str:
    """LLM 不可用时用模板拼装降级报告（保证报告仍可交付）。"""
    if ctx.error_rate > 0 or ctx.samples > 0:
        conclusion = (
            f"本次执行 TPS={ctx.tps}、P95={ctx.p95_ms}ms、错误率={ctx.error_rate}%。"
            f"建议关注错误率与 P95 延迟，进一步分析瓶颈。"
        )
    else:
        conclusion = "数据不可用：ES 无此执行的指标数据，无法给出结论。"
    return _FALLBACK_TEMPLATE.format(
        run_no=ctx.run_no,
        scenario_name=ctx.scenario_name,
        scenario_type=ctx.scenario_type,
        run_status=ctx.run_status,
        agent_count=ctx.agent_count,
        expected_results=ctx.expected_results,
        samples=ctx.samples,
        success=ctx.success,
        errors=ctx.errors,
        tps=ctx.tps,
        p95_ms=ctx.p95_ms,
        error_rate=ctx.error_rate,
        metrics_source=ctx.metrics_source,
        conclusion=conclusion,
    )


def _is_llm_configured() -> bool:
    """判定 LLM 是否为真实模型（非 FakeListChatModel 降级）。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    return not isinstance(get_llm(), FakeListChatModel)


async def generate_report(run_no: str, db: AsyncSession) -> dict[str, Any]:
    """端到端报告生成：DB+ES 聚合 → LLM/模板 → MinIO。

    返回 ReportGenerateOut 的 dict 形态（run_no/report_key/confidence/notes）。
    LLM 调用失败 → 降级模板生成，notes 标注；MinIO 写入失败 → 报告内容仍在
    notes 中返回（不阻断 API 响应）。
    """
    ctx = await _gather_context(run_no, db)

    markdown: str
    confidence: float
    notes: str

    if _is_llm_configured():
        try:
            llm = get_llm()
            prompt = _build_prompt(ctx)
            raw = await llm.ainvoke(prompt)
            content = getattr(raw, "content", str(raw))
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            markdown = content
            confidence = 0.85
            notes = "LLM 生成报告"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"报告生成 LLM 调用失败，降级模板: {type(exc).__name__}: {exc}"
            )
            markdown = _build_fallback_markdown(ctx)
            confidence = 0.5
            notes = f"LLM 调用失败，已用模板降级: {type(exc).__name__}"
    else:
        markdown = _build_fallback_markdown(ctx)
        confidence = 0.5
        notes = "LLM 未配置，使用模板降级生成"

    report_key = f"reports/{run_no}/llm-report.md"
    try:
        await storage.upload_bytes(
            report_key,
            markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        logger.info(f"LLM 报告已写入 MinIO: {report_key} ({len(markdown)} chars)")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"报告写入 MinIO 失败，内容仅返回不落盘: {type(exc).__name__}: {exc}"
        )
        notes = f"{notes}; MinIO 写入失败: {type(exc).__name__}"

    return {
        "run_no": run_no,
        "report_key": report_key,
        "confidence": confidence,
        "notes": notes,
    }
