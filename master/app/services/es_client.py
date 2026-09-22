"""Elasticsearch 客户端：指标写入与聚合查询。

索引约定（与 docs/tech-selection.md 第 5 节一致）：
- pt-metrics-yyyy.MM.dd  5s 粒度时序（run_no/agent_id/label 维度）
- pt-summary             执行汇总（run_no 为 keyword，单索引避免小分片泛滥）
"""

import time
from datetime import datetime, timezone

from elasticsearch import AsyncElasticsearch, BadRequestError
from loguru import logger

from app.core.config import get_settings
from app.metrics import record_es_write

_es: AsyncElasticsearch | None = None

_METRICS_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "run_no": {"type": "keyword"},
            "agent_id": {"type": "keyword"},
            "label": {"type": "keyword"},
            "sample_type": {"type": "keyword"},
            "@timestamp": {"type": "date"},
            "interval_tps": {"type": "float"},
            "avg_rt": {"type": "float"},
            "min_rt": {"type": "float"},
            "max_rt": {"type": "float"},
            "p95_rt": {"type": "float"},
            "err_rate": {"type": "float"},
            "samples": {"type": "long"},
            "success": {"type": "long"},
            "errors": {"type": "long"},
            "threads": {"type": "integer"},
        }
    },
}

# pt-summary 索引的显式 mapping。run_no 必须 keyword，否则 term 查询无法
# 精确匹配（text 字段会被标准分词器拆分 'r...-xxxxx' 为多个 token）。
# 历史已创建的 pt-summary 索引无 mapping，由 ensure_indices 用 put_mapping
# 兜底；但对已存在的 text 字段无法改类型，仍需重建索引修复存量数据。
_SUMMARY_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "run_no": {"type": "keyword"},
        }
    },
}


def get_es() -> AsyncElasticsearch:
    global _es
    if _es is None:
        _es = AsyncElasticsearch(hosts=[get_settings().es_url], request_timeout=10)
    return _es


async def ensure_indices() -> None:
    """创建索引模板与汇总索引（幂等，启动期调用）。

    模板用 PUT 覆盖写入（幂等 upsert），保证代码中的最新映射始终生效；
    汇总索引做存在性检查后再建（带显式 mapping），已存在的索引用
    put_mapping 兜底（对已存在的 text 字段无法改类型，仅新增字段生效）。
    """
    settings = get_settings()
    es = get_es()
    template_name = f"{settings.es_index_prefix}-metrics-template"
    await es.indices.put_index_template(
        name=template_name,
        index_patterns=[settings.metrics_index_pattern],
        template=_METRICS_MAPPING,
    )
    logger.info(f"已写入指标索引模板 {template_name}")
    summary_props = _SUMMARY_MAPPING["mappings"]["properties"]
    if await es.indices.exists(index=settings.summary_index):
        # 对已存在索引做 mapping 兜底：能新增字段，但 run_no 若已被动态
        # 映射成 text 则 put_mapping 会抛 BadRequestError（ES 不允许改字段
        # 类型）。此时降级为 warning——query_summary 用 match_phrase 在
        # text 字段上同样能工作，无需重建索引即可正常查询。
        try:
            await es.indices.put_mapping(
                index=settings.summary_index, properties=summary_props
            )
            logger.info(f"已同步汇总索引 mapping {settings.summary_index}")
        except BadRequestError as exc:
            logger.warning(
                f"汇总索引 {settings.summary_index} mapping 同步失败（已存在字"
                f"段类型冲突，查询降级 match_phrase 不影响功能）：{exc}"
            )
    else:
        await es.indices.create(index=settings.summary_index, body=_SUMMARY_MAPPING)
        logger.info(f"已创建汇总索引 {settings.summary_index}")


def _metrics_index_for_ts(ts_seconds: int) -> str:
    day = datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y.%m.%d")
    return f"{get_settings().es_index_prefix}-metrics-{day}"


async def write_metrics(doc: dict) -> None:
    """写入单条 5s 聚合指标。量级上来后改造为 bulk 批量写。"""
    start = time.perf_counter()
    ok = True
    try:
        logger.debug(
            f"write_metrics: run={doc.get('run_no', '?')} "
            f"agent={doc.get('agent_id', '?')} labels={len(doc.get('by_label', []) or [])}"
        )
        ts = int(doc.get("ts") or datetime.now(tz=timezone.utc).timestamp())
        body = {k: v for k, v in doc.items() if k != "ts"}
        body["@timestamp"] = ts * 1000
        # 无 label 维度时归为整体；sample_type=request|transaction（事务/请求行级
        # 区分，Agent 解析 JTL 官方标记所得），_total 聚合文档不带 sample_type
        for item in body.pop("by_label", []) or []:
            await get_es().index(
                index=_metrics_index_for_ts(ts),
                document={
                    **body,
                    "label": item.get("label", "_total"),
                    **{
                        k: item[k]
                        for k in (
                            "sample_type",
                            "interval_tps",
                            "avg_rt",
                            "min_rt",
                            "max_rt",
                            "p95_rt",
                            "err_rate",
                            "samples",
                            "success",
                            "errors",
                        )
                        if k in item
                    },
                },
            )
        await get_es().index(
            index=_metrics_index_for_ts(ts), document={**body, "label": "_total"}
        )
    except Exception:
        ok = False
        raise
    finally:
        record_es_write("write_metrics", time.perf_counter() - start, ok=ok)


async def write_summary(run_no: str, summary: dict) -> None:
    start = time.perf_counter()
    ok = True
    try:
        logger.debug(f"write_summary: run={run_no} keys={list(summary.keys())}")
        await get_es().index(
            index=get_settings().summary_index,
            document={"run_no": run_no, **summary},
        )
    except Exception:
        ok = False
        raise
    finally:
        record_es_write("write_summary", time.perf_counter() - start, ok=ok)


async def query_summary(run_no: str) -> dict | None:
    """按 run_no 读取 pt-summary 汇总文档（write_summary 写入结构原样透出）。

    run 每次收官只写一条，size=1 足够；无文档（执行尚未结束、历史数据
    缺失或 ES 丢数）返回 None，由调用方区分提示。
    返回体剥离 run_no（路径已携带）。

    查询用 match_phrase 而非 term：历史 pt-summary 索引在显式 mapping
    引入前已被 ES 动态映射为 text（标准分词器按 '-' 拆分），term 查 text
    字段无法精确匹配整串；match_phrase 在 text 字段上做分词后的短语匹配
    （要求 token 序列与位置完全一致），在 keyword 字段上整串匹配，
    对存量 text 索引与新建 keyword 索引均兼容。
    """
    resp = await get_es().search(
        body={"size": 1, "query": {"match_phrase": {"run_no": run_no}}},
        index=get_settings().summary_index,
    )
    hits = (resp.get("hits", {}) or {}).get("hits", []) or []
    if not hits:
        return None
    src = dict(hits[0].get("_source", {}) or {})
    src.pop("run_no", None)
    return src


async def query_realtime_summary(run_no: str) -> dict:
    """对 pt-metrics-* 做一次聚合，返回与终态 summary 同结构的实时汇总。

    口径：
    - samples/success/errors：sum（与终态一致）
    - min_rt/max_rt：min/max（与终态一致；0 视为未上报，但 min 口径取 ES
      原值，出现 0 时如实返回，调用方需自行容错）
    - avg_rt：按 samples 加权（sum(avg_rt*samples)/sum(samples)），与终态一致
    - p95_rt：用 ES tdigest percentiles 聚合，近似值（与 Agent 终态精确值
      通常偏差 <1%，仅作实时观察用）
    - avg_tps：窗口口径 sum(samples)*1000/(max(@timestamp)-min(@timestamp))，
      与 Agent 终态 _avg_tps 一致；无样本或时间窗口 ≤0 时为 0
    - by_label：按 (label, sample_type) 二级分桶，与终态结构对齐；
      排除 _total 行（其数据已并入顶层聚合）
    返回结构：{samples,success,errors,min_rt,max_rt,avg_rt,p95_rt,avg_tps,
              by_label[{label,sample_type,samples,success,errors,
                         min_rt,max_rt,avg_rt,p95_rt,avg_tps}]}
    无任何 metrics 文档时返回全零汇总（不抛异常，由调用方决定如何呈现）。
    """
    # 顶层聚合：跨所有 label（含 _total）的总量、窗口、p95
    body = {
        "size": 0,
        "query": {"term": {"run_no": run_no}},
        "aggs": {
            # 仅聚合非 _total 文档，避免顶层与 by_label 双计
            "non_total": {
                "filter": {"bool": {"must_not": [{"term": {"label": "_total"}}]}},
                "aggs": {
                    "samples": {"sum": {"field": "samples"}},
                    "success": {"sum": {"field": "success"}},
                    "errors": {"sum": {"field": "errors"}},
                    "min_rt": {"min": {"field": "min_rt"}},
                    "max_rt": {"max": {"field": "max_rt"}},
                    "p95_rt": {
                        "percentiles": {
                            "field": "p95_rt",
                            "percents": [95],
                            "tdigest": {"compression": 100},
                        }
                    },
                    "first_ts": {"min": {"field": "@timestamp"}},
                    "last_ts": {"max": {"field": "@timestamp"}},
                    # 加权平均 avg_rt：avg_rt*samples 求和 / samples 求和
                    "rt_weighted": {
                        "sum": {
                            "script": {
                                "source": "doc['avg_rt'].size()==0 || "
                                "doc['samples'].size()==0 ? 0 : "
                                "doc['avg_rt'].value * doc['samples'].value",
                            }
                        }
                    },
                    "by_label": {
                        "terms": {"field": "label", "size": 50},
                        "aggs": {
                            "by_type": {
                                "terms": {
                                    "field": "sample_type",
                                    "size": 3,
                                    "missing": "request",
                                },
                                "aggs": {
                                    "samples": {"sum": {"field": "samples"}},
                                    "success": {"sum": {"field": "success"}},
                                    "errors": {"sum": {"field": "errors"}},
                                    "min_rt": {"min": {"field": "min_rt"}},
                                    "max_rt": {"max": {"field": "max_rt"}},
                                    "p95_rt": {
                                        "percentiles": {
                                            "field": "p95_rt",
                                            "percents": [95],
                                        }
                                    },
                                    "first_ts": {"min": {"field": "@timestamp"}},
                                    "last_ts": {"max": {"field": "@timestamp"}},
                                    "rt_weighted": {
                                        "sum": {
                                            "script": {
                                                "source": "doc['avg_rt'].size()==0 "
                                                "|| doc['samples'].size()==0 ? 0 "
                                                ": doc['avg_rt'].value * "
                                                "doc['samples'].value",
                                            }
                                        }
                                    },
                                },
                            }
                        },
                    },
                },
            }
        },
    }
    resp = await get_es().search(
        body=body, index=f"{get_settings().es_index_prefix}-metrics-*"
    )
    non_total = (resp.get("aggregations", {}) or {}).get("non_total", {}) or {}
    samples = int(non_total.get("samples", {}).get("value") or 0)
    success = int(non_total.get("success", {}).get("value") or 0)
    errors = int(non_total.get("errors", {}).get("value") or 0)
    min_rt = float(non_total.get("min_rt", {}).get("value") or 0.0)
    max_rt = float(non_total.get("max_rt", {}).get("value") or 0.0)
    rt_weighted = float(non_total.get("rt_weighted", {}).get("value") or 0.0)
    avg_rt = rt_weighted / samples if samples > 0 else 0.0
    p95_values = (non_total.get("p95_rt", {}) or {}).get("values", {}) or {}
    p95_rt = float(p95_values.get("95.0") or 0.0)
    first_ts = non_total.get("first_ts", {}).get("value")
    last_ts = non_total.get("last_ts", {}).get("value")
    avg_tps = _avg_tps_from_window(samples, first_ts, last_ts)

    by_label: list[dict] = []
    for label_bucket in (non_total.get("by_label", {}) or {}).get("buckets", []):
        label = label_bucket.get("key")
        for type_bucket in (label_bucket.get("by_type", {}) or {}).get("buckets", []):
            lbl_samples = int(type_bucket.get("samples", {}).get("value") or 0)
            lbl_errors = int(type_bucket.get("errors", {}).get("value") or 0)
            lbl_success = int(type_bucket.get("success", {}).get("value") or 0)
            lbl_rt_weighted = float(
                type_bucket.get("rt_weighted", {}).get("value") or 0.0
            )
            lbl_p95_values = (type_bucket.get("p95_rt", {}) or {}).get(
                "values", {}
            ) or {}
            lbl_first_ts = type_bucket.get("first_ts", {}).get("value")
            lbl_last_ts = type_bucket.get("last_ts", {}).get("value")
            by_label.append(
                {
                    "label": label,
                    "sample_type": type_bucket.get("key"),
                    "samples": lbl_samples,
                    "success": lbl_success,
                    "errors": lbl_errors,
                    "min_rt": float(type_bucket.get("min_rt", {}).get("value") or 0.0),
                    "max_rt": float(type_bucket.get("max_rt", {}).get("value") or 0.0),
                    "avg_rt": lbl_rt_weighted / lbl_samples if lbl_samples > 0 else 0.0,
                    "p95_rt": float(lbl_p95_values.get("95.0") or 0.0),
                    "avg_tps": _avg_tps_from_window(
                        lbl_samples, lbl_first_ts, lbl_last_ts
                    ),
                }
            )
    by_label.sort(key=lambda x: x["samples"], reverse=True)
    return {
        "samples": samples,
        "success": success,
        "errors": errors,
        "min_rt": round(min_rt, 2),
        "max_rt": round(max_rt, 2),
        "avg_rt": round(avg_rt, 2),
        "p95_rt": round(p95_rt, 2),
        "avg_tps": round(avg_tps, 2),
        "by_label": by_label,
    }


def _normalize_run_metrics(raw: dict, samples: int, source: str) -> dict:
    """把 pt-summary.summary / 实时聚合结构归一化为 FR-08 基准指标。

    返回 {tps, p95_ms, error_rate, samples, source}：
    - tps ← avg_tps；p95_ms ← p95_rt（ms）
    - error_rate 为百分比（0-100，errors/samples*100），与 LLM 回答中
      "错误率 0.5%" 的百分数口径一致；guardrail 对无百分号小数按比率换算
    """
    errors = int(raw.get("errors") or 0)
    return {
        "tps": round(float(raw.get("avg_tps") or 0.0), 2),
        "p95_ms": round(float(raw.get("p95_rt") or 0.0), 2),
        "error_rate": round(errors / samples * 100, 4) if samples else 0.0,
        "samples": samples,
        "source": source,
    }


async def get_run_metrics(run_no: str) -> dict | None:
    """FR-08 指标校验基准：返回标准化 {tps,p95_ms,error_rate,samples,source}。

    优先终态 pt-summary（run 已收官）；终态缺失时兜底 pt-metrics 实时聚合
    （进行中 run 也可校验）；两者均无样本数据（run_no 不存在/ES 丢数）返回
    None，由 guardrail 跳过校验并在 notes 标注"无法获取基准指标"（SRS FR-08
    异常处理 + 风险表"指标校验无基准数据 → 跳过校验"）。
    """
    summary_doc = await query_summary(run_no)
    if summary_doc is not None:
        summary = summary_doc.get("summary") or {}
        samples = int(summary.get("samples") or 0)
        if samples > 0:
            return _normalize_run_metrics(summary, samples, "summary")

    realtime = await query_realtime_summary(run_no)
    samples = int(realtime.get("samples") or 0)
    if samples > 0:
        return _normalize_run_metrics(realtime, samples, "realtime")
    return None


def _avg_tps_from_window(
    samples: int, first_ts: float | None, last_ts: float | None
) -> float:
    """窗口口径 TPS：samples*1000/(last_ts-first_ts)，时间单位 ms。

    与 Agent 终态 jtl_parser._avg_tps 一致：用首末样本时间戳作为窗口边界。
    无样本或时间窗口 ≤0 时返回 0（首末样本同毫秒也视为无效窗口）。
    """
    if samples <= 0 or first_ts is None or last_ts is None:
        return 0.0
    duration_ms = float(last_ts) - float(first_ts)
    if duration_ms <= 0:
        return 0.0
    return samples * 1000.0 / duration_ms


async def query_timeseries(
    run_no: str,
    start_ts: int,
    end_ts: int,
    interval_s: int = 15,
    sample_type: str | None = None,
) -> list[dict]:
    """按 label（及 sample_type）维度聚合时间序列，拍平为前端直接消费的点列表。

    返回：[{"ts": <unix秒>, "label": str, "sample_type": str,
           "tps": float, "avg_rt": float(ms), "error_rate": float(百分比),
           "min_rt": float(ms), "max_rt": float(ms),
           "samples": int, "success": int, "errors": int}]
    sample_type 传 request/transaction 时仅聚合对应类型文档；label 与
    sample_type 二级分桶，事务与取样器同名的曲线不会互相污染。
    min_rt/max_rt 取桶内各 5s 批次的最小/最大（口径与批次一致）；
    samples/errors 为桶内求和，success 由 samples-errors 推导。
    注意：ES 中 err_rate 存的是 0~1 比率（errors/samples），此处 *100 转百分比；
    ts 取 date_histogram 桶 key（epoch 毫秒）//1000，绝对时区无关；
    无 sample_type 的文档（_total 聚合文档/旧数据）落入 "_total" 类型桶。
    """
    filters: list[dict] = [
        {"term": {"run_no": run_no}},
        {"range": {"@timestamp": {"gte": start_ts * 1000, "lte": end_ts * 1000}}},
    ]
    if sample_type:
        filters.append({"term": {"sample_type": sample_type}})
    body = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "by_label": {
                "terms": {"field": "label", "size": 50},
                "aggs": {
                    "by_type": {
                        "terms": {
                            "field": "sample_type",
                            "size": 3,
                            "missing": "_total",
                        },
                        "aggs": {
                            "over_time": {
                                "date_histogram": {
                                    "field": "@timestamp",
                                    "fixed_interval": f"{interval_s}s",
                                },
                                "aggs": {
                                    "tps": {"avg": {"field": "interval_tps"}},
                                    "rt": {"avg": {"field": "avg_rt"}},
                                    "min_rt": {"min": {"field": "min_rt"}},
                                    "max_rt": {"max": {"field": "max_rt"}},
                                    "err": {"avg": {"field": "err_rate"}},
                                    "samples": {"sum": {"field": "samples"}},
                                    "errors": {"sum": {"field": "errors"}},
                                },
                            }
                        },
                    }
                },
            }
        },
    }
    resp = await get_es().search(
        body=body, index=f"{get_settings().es_index_prefix}-metrics-*"
    )

    # 拍平 ES 嵌套聚合为点列表：by_label.buckets[].by_type.buckets[].over_time.buckets[]
    points: list[dict] = []
    for label_bucket in (
        (resp.get("aggregations", {}) or {}).get("by_label", {}).get("buckets", [])
    ):
        label = label_bucket.get("key")
        for type_bucket in (label_bucket.get("by_type", {}) or {}).get("buckets", []):
            stype = type_bucket.get("key")
            for tb in (type_bucket.get("over_time", {}) or {}).get("buckets", []):
                samples = int(tb.get("samples", {}).get("value") or 0)
                errors = int(tb.get("errors", {}).get("value") or 0)
                points.append(
                    {
                        "ts": int(tb["key"]) // 1000,
                        "label": label,
                        "sample_type": stype,
                        "tps": round(tb.get("tps", {}).get("value") or 0.0, 3),
                        "avg_rt": round(tb.get("rt", {}).get("value") or 0.0, 2),
                        "min_rt": round(tb.get("min_rt", {}).get("value") or 0.0, 2),
                        "max_rt": round(tb.get("max_rt", {}).get("value") or 0.0, 2),
                        "error_rate": round(
                            (tb.get("err", {}).get("value") or 0.0) * 100, 2
                        ),
                        "samples": samples,
                        "success": samples - errors,
                        "errors": errors,
                    }
                )
    points.sort(key=lambda p: (p["ts"], str(p["label"]), str(p["sample_type"])))
    return points
