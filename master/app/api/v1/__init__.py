"""v1 路由汇总。"""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    assets,
    auth,
    chat,
    environments,
    health,
    members,
    metrics,
    plugins,
    projects,
    runs,
    scenarios,
    schedules,
    scripts,
    test_plans,
    transactions,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(agents.router, tags=["agents"])
api_router.include_router(plugins.router, tags=["plugins"])
api_router.include_router(scripts.router, tags=["scripts"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(environments.router, tags=["environments"])
api_router.include_router(transactions.router, tags=["transactions"])
api_router.include_router(test_plans.router, tags=["test-plans"])
api_router.include_router(assets.router, tags=["assets"])
api_router.include_router(members.router, tags=["project-members"])
api_router.include_router(users.router, tags=["users"])
api_router.include_router(scenarios.router, tags=["scenarios"])
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(schedules.router, tags=["schedules"])
api_router.include_router(metrics.router, tags=["metrics"])
api_router.include_router(chat.router, tags=["llm-chat"])
