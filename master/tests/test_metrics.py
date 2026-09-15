"""/metrics 端点契约测试：验证 Prometheus 抓取格式与关键指标存在。"""

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_metrics_endpoint_exposes_prometheus_format() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    ) as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    # Prometheus 文本格式必须含 # HELP / # TYPE 头
    assert "# HELP" in body and "# TYPE" in body
    # HTTP RT 指标必须存在（setup_metrics 注册的 Histogram）
    assert "http_request_duration_seconds" in body
    # ES 写入指标必须存在（es_client 埋点）
    assert "ptp_es_write_duration_seconds" in body
    # 业务级 Gauge 必须存在（_PlatformCollector 暴露）
    assert "ptp_db_pool" in body
    assert "ptp_scheduler_active_jobs" in body
    assert "ptp_agent_online_count" in body


async def test_http_request_records_duration_metric() -> None:
    """触发一次健康检查请求后，/metrics 必须反映该请求的 RT 观测值。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    ) as client:
        # 先打一次 /api/v1/health 触发中间件埋点
        await client.get("/api/v1/health")
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    # FastAPI include_router(prefix="/api/v1") 不会把 prefix 拼到
    # scope["route"].path 上（FastAPI 内部行为），所以 handler 标签是
    # 路由本身定义的 path（/health），监控埋点工作正常
    assert 'handler="/health"' in resp.text
