"""Prometheus 指标定义与采集器。

设计要点（避免侵入业务代码）：
- HTTP RT/吞吐/错误：用轻量 BaseHTTPMiddleware 拦截，从 request.scope["route"]
  取路径模板（如 /api/v1/runs/{run_no}/summary），避免高基数标签爆炸
- DB 连接池占用、scheduler 活跃任务数、Agent 在线数：用自定义 Collector
  在每次抓取时回调 getter，零业务代码侵入
- ES 写入耗时：唯一需要在业务路径内 observe 的指标，es_client.py
  调用 record_es_write()

依赖只引 prometheus-client（不引 prometheus-fastapi-instrumentator）：
- 减少一个第三方包
- 标签集合完全自控（method/handler/status）
- 中间件只对 REST 路由生效，/metrics 自身不参与统计（避免递归计数）
"""

import re
import time

from prometheus_client import Counter, Histogram, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# 全局指标（注册到默认 REGISTRY，/metrics 端点统一暴露）
http_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    labelnames=["handler", "method", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

http_requests_total = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    labelnames=["handler", "method", "status"],
)

es_write_duration = Histogram(
    "ptp_es_write_duration_seconds",
    "ES 写入耗时（秒）",
    labelnames=["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

es_write_total = Counter(
    "ptp_es_write_total",
    "ES 写入次数",
    labelnames=["operation"],
)

es_write_errors = Counter(
    "ptp_es_write_errors_total",
    "ES 写入失败次数",
    labelnames=["operation"],
)


# ---------- P3 LLM 埋点（NFR-03：复用已有 Prometheus 埋点） ----------
# 4 个 LLM 指标，对齐 SRS 8.1 KPI 评测维度：

llm_call_duration = Histogram(
    "ptp_llm_call_duration_seconds",
    "LLM 调用耗时（秒）",
    labelnames=["endpoint"],  # chat / chat_stream / ws_chat / report
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

llm_calls_total = Counter(
    "ptp_llm_calls_total",
    "LLM 调用总数",
    labelnames=["endpoint", "status"],  # status: success / degraded / fallback
)

llm_retrieval_score = Histogram(
    "ptp_llm_retrieval_score",
    "RAG 检索 Top-k 相似度分（0-1）",
    labelnames=["project_id"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

llm_guardrail_result = Counter(
    "ptp_llm_guardrail_result_total",
    "FR-08 指标校验结果计数",
    labelnames=["result"],  # matched / mismatched / skipped / na
)


def record_llm_call(endpoint: str, duration: float, status: str = "success") -> None:
    """LLM 调用埋点（chat/ws/report 路径调用）。

    endpoint: chat / chat_stream / ws_chat / report
    status: success（正常 LLM）/ degraded（降级响应）/ fallback（兜底模板）
    """
    llm_call_duration.labels(endpoint=endpoint).observe(duration)
    llm_calls_total.labels(endpoint=endpoint, status=status).inc()


def record_llm_retrieval(project_id: int, score: float) -> None:
    """检索 Top-k 命中埋点（knowledge-search / RAG 上下文检索调用）。"""
    llm_retrieval_score.labels(project_id=str(project_id)).observe(score)


def record_llm_guardrail(result: str) -> None:
    """指标校验结果埋点（guardrail 调用）。

    result: matched（一致）/ mismatched（超差）/ skipped（无基准跳过）/ na
    """
    llm_guardrail_result.labels(result=result).inc()


# 复用对象，避免每次请求重新构造
_STATUS_RE = re.compile(r"Checkedout:\s*(\d+).*Checkedin:\s*(\d+).*Pool size:\s*(\d+)")


def _parse_pool_status(status: str) -> tuple[int, int, int]:
    """解析 SQLAlchemy QueuePool.status() 字符串为 (checkedout, checkedin, size)。"""
    m = _STATUS_RE.search(status)
    if not m:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


class _PlatformCollector:
    """自定义 Collector：抓取时回调业务模块读取运行时状态。

    所有回调为同步内存操作（pool.status() / get_jobs() / connected_ids()），
    不触碰 DB/IO，开销 <1ms，不会阻塞事件循环。
    """

    def collect(self):
        # MySQL 连接池
        yield from self._collect_db_pool()
        # APScheduler 活跃任务数
        yield from self._collect_scheduler_jobs()
        # Agent 在线数
        yield from self._collect_agent_online()

    @staticmethod
    def _collect_db_pool():
        try:
            from app.db.session import engine

            checkedout, checkedin, size = _parse_pool_status(engine.pool.status())
        except Exception:  # noqa: BLE001
            checkedout = checkedin = size = 0
        g = GaugeMetricFamily(
            "ptp_db_pool",
            "MySQL 连接池状态",
            labels=["metric"],
        )
        g.add_metric(["checkedout"], checkedout)
        g.add_metric(["checkedin"], checkedin)
        g.add_metric(["size"], size)
        yield g

    @staticmethod
    def _collect_scheduler_jobs():
        try:
            from app.services.scheduler import get_active_job_count

            count = get_active_job_count()
        except Exception:  # noqa: BLE001
            count = 0
        yield GaugeMetricFamily(
            "ptp_scheduler_active_jobs",
            "APScheduler 活跃任务数",
            value=count,
        )

    @staticmethod
    def _collect_agent_online():
        try:
            from app.ws.manager import agent_manager

            count = len(agent_manager.connected_ids())
        except Exception:  # noqa: BLE001
            count = 0
        yield GaugeMetricFamily(
            "ptp_agent_online_count",
            "在线 Agent 数",
            value=count,
        )


# 注册到默认 REGISTRY，prometheus_client 的 make_asgi_app() 自动暴露
_COLLECTOR_REGISTERED = False


def _ensure_collector_registered() -> None:
    """幂等注册自定义 Collector（测试多次 import 不重复注册）。"""
    global _COLLECTOR_REGISTERED
    if _COLLECTOR_REGISTERED:
        return
    from prometheus_client import REGISTRY

    REGISTRY.register(_PlatformCollector())
    _COLLECTOR_REGISTERED = True


class MetricsMiddleware(BaseHTTPMiddleware):
    """HTTP 请求 RT / 吞吐 / 状态码 采集。

    路径分组用路由模板（/runs/{run_no}/summary 而非实际 run_no），
    避免高基数标签爆炸；未匹配到路由（404）回退到 raw path。
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        path = request.url.path
        # 跳过 /metrics 自身，避免监控反向污染指标
        if path == "/metrics":
            return response
        route = request.scope.get("route")
        if route is not None and hasattr(route, "path"):
            handler = route.path
        else:
            handler = path
        labels = {
            "handler": handler,
            "method": request.method,
            "status": str(response.status_code),
        }
        http_duration.labels(**labels).observe(duration)
        http_requests_total.labels(**labels).inc()
        return response


def record_es_write(operation: str, duration: float, ok: bool = True) -> None:
    """业务侧 ES 写入埋点（es_client 调用）。

    operation 取值：write_metrics / write_summary / ensure_indices
    duration 单位秒，由调用方用 perf_counter 计时后传入
    """
    es_write_duration.labels(operation=operation).observe(duration)
    es_write_total.labels(operation=operation).inc()
    if not ok:
        es_write_errors.labels(operation=operation).inc()


def setup_metrics(app) -> None:
    """FastAPI 集成入口：注册中间件 + 挂载 /metrics 端点。

    必须在 app.include_router 之后调用，中间件才能拿到路由 scope["route"]。
    """
    _ensure_collector_registered()
    app.add_middleware(MetricsMiddleware)
    app.mount("/metrics", make_asgi_app(), name="metrics")


def reset_for_test() -> None:
    """测试用：清空自定义 Collector 注册标志，便于重复 setup。"""
    global _COLLECTOR_REGISTERED
    _COLLECTOR_REGISTERED = False
