"""项目管理契约测试：鉴权门禁 + 请求体校验（不落库，与现有测试基础设施一致）。"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.security import create_access_token
from app.main import app
from app.schemas import ProjectIn

_TOKEN = create_access_token("tester", "viewer")


async def test_create_project_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/projects", json={"name": "新项目"})
    assert resp.status_code == 401


async def test_list_projects_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/projects")
    assert resp.status_code == 401


async def test_list_projects_invalid_page_rejected() -> None:
    # page=0 违反 ge=1：422，且在查询参数校验阶段失败，不会访问数据库
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_projects_page_size_over_limit_rejected() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects?page_size=101",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_create_project_missing_name_rejected() -> None:
    # name 缺失：422，且在校验阶段失败，不会访问数据库
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects",
            json={"description": "缺名称"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_create_project_blank_name_rejected() -> None:
    # 纯空白名称：schema 校验失败 → 422
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects",
            json={"name": "   "},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


def test_project_in_strips_name() -> None:
    payload = ProjectIn(name="  电商交易链路压测  ", description="x")
    assert payload.name == "电商交易链路压测"


def test_project_in_blank_name_raises() -> None:
    with pytest.raises(ValidationError):
        ProjectIn(name="\t ")
