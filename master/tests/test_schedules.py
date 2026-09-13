"""定时任务契约测试：项目嵌套路由的鉴权门禁、参数校验与旧路径下线（不落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import app

_TOKEN = create_access_token("tester", "viewer")

_CLIENT_KWARGS = {"transport": ASGITransport(app=app), "base_url": "http://test"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/projects/1/schedules"),
        ("POST", "/api/v1/projects/1/schedules"),
        ("POST", "/api/v1/projects/1/schedules/1/toggle"),
    ],
)
async def test_scoped_schedule_endpoints_require_token(method: str, path: str) -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/schedules"),
        ("POST", "/api/v1/schedules"),
        ("POST", "/api/v1/schedules/1/toggle"),
    ],
)
async def test_legacy_flat_schedule_paths_removed(method: str, path: str) -> None:
    # 旧扁平路径已下线：无路由匹配 → 404
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 404


async def test_create_schedule_missing_fields_rejected() -> None:
    # name/cron/scenario_id 必填：422 在进入 handler 前失败，不会访问数据库
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.post(
            "/api/v1/projects/1/schedules",
            json={},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_schedules_invalid_project_id_type_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/not-an-int/schedules",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_schedules_invalid_page_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/1/schedules?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422
