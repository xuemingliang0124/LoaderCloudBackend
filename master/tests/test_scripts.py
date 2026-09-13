"""脚本管理契约测试：项目嵌套路由的鉴权门禁、参数校验与旧路径下线（不落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import app

_TOKEN = create_access_token("tester", "viewer")

_CLIENT_KWARGS = {"transport": ASGITransport(app=app), "base_url": "http://test"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/projects/1/scripts"),
        ("POST", "/api/v1/projects/1/scripts"),
        ("PUT", "/api/v1/projects/1/scripts/1/jmx"),
        ("DELETE", "/api/v1/projects/1/scripts/1"),
        ("GET", "/api/v1/projects/1/scripts/1/thread-groups"),
    ],
)
async def test_scoped_script_endpoints_require_token(method: str, path: str) -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/v1/scripts"), ("POST", "/api/v1/scripts")]
)
async def test_legacy_flat_script_paths_removed(method: str, path: str) -> None:
    # 旧扁平路径已下线：无路由匹配 → 404
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 404


async def test_upload_missing_file_rejected() -> None:
    # multipart 必传 file：422 在进入 handler（项目存在性校验）前失败，不访问数据库
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.post(
            "/api/v1/projects/1/scripts",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_scripts_invalid_project_id_type_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/not-an-int/scripts",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_scripts_invalid_page_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/1/scripts?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422
