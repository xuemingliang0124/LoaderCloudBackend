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
            "@timestamp": {"type": "date"},
            "interval_tps": {"type": "float"},
            "avg_rt": {"type": "float"},
            "p95_rt": {"type": "float"},
            "err_rate": {"type": "float"},
            "samples": {"type": "long"},
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
    # 无 label 维度时归为整体
    for item in body.pop("by_label", []) or []:
        await get_es().index(
            index=_metrics_index_for_ts(ts),
            document={**body, "label": item.get("label", "_total"), **{
                k: item[k] for k in ("interval_tps", "avg_rt", "p95_rt", "err_rate", "errors") if k in item
            }},
        )
    await get_es().index(index=_metrics_index_for_ts(ts), document={**body, "label": "_total"})


async def write_summary(run_no: str, summary: dict) -> None:
    await get_es().index(
        index=get_settings().summary_index, document={"run_no": run_no, **summary}
    )


async def query_timeseries(
    run_no: str, start_ts: int, end_ts: int, interval_s: int = 15
) -> dict:
    """按 label 维度聚合时间序列，供前端曲线渲染。"""
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"run_no": run_no}},
                    {"range": {"@timestamp": {"gte": start_ts * 1000, "lte": end_ts * 1000}}},
                ]
            }
        },
        "aggs": {
            "by_label": {
                "terms": {"field": "label", "size": 50},
                "aggs": {
                    "over_time": {
                        "date_histogram": {
                            "field": "@timestamp",
                            "fixed_interval": f"{interval_s}s",
                        },
                        "aggs": {
                            "tps": {"avg": {"field": "interval_tps"}},
                            "rt": {"avg": {"field": "avg_rt"}},
                            "err": {"avg": {"field": "err_rate"}},
                        },
                    }
                },
            }
        },
    }
    resp = await get_es().search(body=body, index=f"{get_settings().es_index_prefix}-metrics-*")
    return resp.get("aggregations", {})
