"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    auth,
    health,
    metrics,
    runs,
    scenarios,
    schedules,
    scripts,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(agents.router, tags=["agents"])
api_router.include_router(scripts.router, tags=["scripts"])
api_router.include_router(scenarios.router, tags=["scenarios"])
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(schedules.router, tags=["schedules"])
api_router.include_router(metrics.router, tags=["metrics"])
