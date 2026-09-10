"""Master 入口：lifespan 初始化 + 路由挂载 + 全局异常处理。

启动顺序：建表(dev 兜底) → 默认用户 → ES 索引 → MinIO bucket → 调度器 → 离线检测。
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.log import logger, setup_logging
from app.db.session import engine
from app.models import Base
from app.services import es_client, orchestrator, storage, user_service
from app.services.agent_registry import mark_stale_agents_offline
from app.services.exceptions import BusinessError
from app.services.scheduler import start_scheduler, stop_scheduler
from app.ws.manager import agent_manager
from app.ws.routes import router as ws_router


async def _offline_check_loop() -> None:
    """周期扫描心跳超时的 Agent 并置 OFFLINE。"""
    while True:
        try:
            offline = await mark_stale_agents_offline(agent_manager.connected_ids())
            if offline:
                logger.warning(f"心跳超时下线 Agent 数: {offline}")
        except Exception:  # noqa: BLE001
            logger.exception("Agent 离线检测异常")
        await asyncio.sleep(10)


async def _retry(desc: str, coro_factory, attempts: int = 15, delay: float = 3.0):
    """启动期依赖重试：ES/MinIO 冷启动较慢，避免容器因未就绪而崩溃退出。"""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(f"等待 {desc} 就绪（{i}/{attempts}）: {exc}")
            await asyncio.sleep(delay)
    logger.error(f"{desc} 在 {attempts} 次重试后仍不可用")
    raise last_exc  # type: ignore[misc]


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.debug)

    async def _init_db():
        async with engine.begin() as conn:
            # dev 兜底建表；生产统一走 alembic（skill 文档 3.4）
            await conn.run_sync(Base.metadata.create_all)

    await _retry("MySQL", _init_db)
    await user_service.ensure_default_user()
    await _retry("Elasticsearch", es_client.ensure_indices)
    await _retry("MinIO", storage.ensure_bucket)
    # 一次性迁移历史脚本级插件到全局插件池（jmeter_script.plugins → jmeter_plugin）
    try:
        from app.services.plugin_sync import recover_legacy_script_plugins

        await recover_legacy_script_plugins()
    except Exception:  # noqa: BLE001
        logger.exception("历史脚本插件迁移失败（不影响启动）")
    await start_scheduler()
    # 从 run_agent_result 恢复 Master 重启前未收尾的执行现场
    try:
        await orchestrator.recover_active_runs()
    except Exception:  # noqa: BLE001
        logger.exception("重启执行现场恢复失败")
    checker = asyncio.create_task(_offline_check_loop())
    logger.info(f"{settings.app_name} 启动完成")
    yield
    checker.cancel()
    stop_scheduler()
    await engine.dispose()


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.include_router(api_router, prefix="/api/v1")
app.include_router(ws_router)


@app.exception_handler(BusinessError)
async def business_error_handler(_: Request, exc: BusinessError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"code": exc.code, "message": exc.message, "data": None},
    )
