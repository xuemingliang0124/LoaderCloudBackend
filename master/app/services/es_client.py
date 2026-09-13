"""Elasticsearch 客户端：指标写入与聚合查询。

索引约定（与 docs/tech-selection.md 第 5 节一致）：
- pt-metrics-yyyy.MM.dd  5s 粒度时序（run_no/agent_id/label 维度）
- pt-summary             执行汇总（run_no 为 keyword，单索引避免小分片泛滥）
"""

from datetime import datetime, timezone

from elasticsearch import AsyncElasticsearch
from loguru import logger

from app.core.config import get_settings

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


def get_es() -> AsyncElasticsearch:
    global _es
    if _es is None:
        _es = AsyncElasticsearch(hosts=[get_settings().es_url], request_timeout=10)
    return _es


async def ensure_indices() -> None:
    """创建索引模板与汇总索引（幂等，启动期调用）。

    模板用 PUT 覆盖写入（幂等 upsert），保证代码中的最新映射始终生效；
    汇总索引做存在性检查后再建。
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
    if not await es.indices.exists(index=settings.summary_index):
        await es.indices.create(index=settings.summary_index)
        logger.info(f"已创建汇总索引 {settings.summary_index}")


def _metrics_index_for_ts(ts_seconds: int) -> str:
    day = datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y.%m.%d")
    return f"{get_settings().es_index_prefix}-metrics-{day}"


async def write_metrics(doc: dict) -> None:
    """写入单条 5s 聚合指标。量级上来后改造为 bulk 批量写。"""
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


async def write_summary(run_no: str, summary: dict) -> None:
    await get_es().index(
        index=get_settings().summary_index, document={"run_no": run_no, **summary}
    )


async def query_summary(run_no: str) -> dict | None:
    """按 run_no 读取 pt-summary 汇总文档（write_summary 写入结构原样透出）。

    run 每次收官只写一条，term 查询 size=1 足够；无文档（执行尚未结束、
    历史数据缺失或 ES 丢数）返回 None，由调用方区分提示。
    返回体剥离 run_no（路径已携带）。
    """
    resp = await get_es().search(
        body={"size": 1, "query": {"term": {"run_no": run_no}}},
        index=get_settings().summary_index,
    )
    hits = (resp.get("hits", {}) or {}).get("hits", []) or []
    if not hits:
        return None
    src = dict(hits[0].get("_source", {}) or {})
    src.pop("run_no", None)
    return src


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
