"""指标查询：ES 聚合透出，供前端曲线渲染。

run 指标属于其归属项目：经 run_no → 场景 → 项目派生归属，
要求项目内 viewer 及以上（与 WS run 流共用 ensure_run_visible），
非成员无法读取他人项目的执行数据。

P3 Stage 5 新增 `GET /metrics/llm`：暴露 4 个 LLM 运营指标
（调用耗时/调用次数/检索相似度/指标校验结果），对齐 SRS 8.1 KPI 评测维度。
"""

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, ensure_run_visible, get_current_user
from app.db.session import get_db
from app.schemas.common import ok
from app.services import es_client

router = APIRouter()


@router.get("/metrics/timeseries")
async def timeseries(
    run_no: str,
    start: int,
    end: int,
    interval: int = 15,
    sample_type: Literal["request", "transaction"] | None = None,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """按 label 维度聚合时间序列（start/end 为 unix 秒）。

    sample_type 可选过滤：request=仅请求、transaction=仅事务；
    缺省时全量返回（点内含 sample_type 字段供前端分组）。
    点字段含 tps/avg_rt/min_rt/max_rt/error_rate 及 samples/success/errors
    （成功/失败笔数）。
    """
    await ensure_run_visible(db, run_no, user)
    return ok(
        await es_client.query_timeseries(
            run_no, start, end, interval, sample_type=sample_type
        )
    )


@router.get("/metrics/llm")
async def llm_metrics(
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """LLM 运营指标汇总（SRS 8.1 KPI 评测维度，NFR-03 Prometheus 埋点）。

    返回 4 个 LLM 指标的当前累积值：
    - call_duration：LLM 调用平均耗时（秒）
    - calls_total：LLM 调用总数（按 status 分桶：success/degraded/fallback）
    - retrieval_score：RAG 检索 Top-k 平均相似度分
    - guardrail_result：FR-08 指标校验结果计数（matched/mismatched/skipped）
    """
    from app.metrics import (
        llm_call_duration,
        llm_calls_total,
        llm_guardrail_result,
        llm_retrieval_score,
    )

    def _histogram_avg(metric) -> float:
        """从 Histogram 的 _buckets 计算加权平均值。"""
        samples = (
            list(metric.collect())[0].samples if hasattr(metric, "collect") else []
        )
        total_sum = 0.0
        total_count = 0
        for s in samples:
            if s.name.endswith("_sum"):
                total_sum = s.value
            if s.name.endswith("_count"):
                total_count = int(s.value)
        return round(total_sum / total_count, 4) if total_count else 0.0

    def _counter_by_label(metric) -> dict[str, float]:
        """Counter 按 labels 拆分为 dict。"""
        result: dict[str, float] = {}
        for sample in metric.collect():
            for s in sample.samples:
                key = "|".join(f"{k}={v}" for k, v in sorted(s.labels.items()))
                result[key] = s.value
        return result

    return ok(
        {
            "call_duration_avg": _histogram_avg(llm_call_duration),
            "calls_total": _counter_by_label(llm_calls_total),
            "retrieval_score_avg": _histogram_avg(llm_retrieval_score),
            "guardrail_result": _counter_by_label(llm_guardrail_result),
        }
    )
