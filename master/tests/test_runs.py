"""执行管理契约测试：项目嵌套路由的鉴权门禁、参数校验与旧路径下线（不落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import app
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario

_TOKEN = create_access_token("tester", "viewer")

_CLIENT_KWARGS = {"transport": ASGITransport(app=app), "base_url": "http://test"}


def _auth(username: str, role: str = "viewer") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/projects/1/runs"),
        ("GET", "/api/v1/projects/1/runs/R20260913000001"),
        ("POST", "/api/v1/projects/1/runs"),
        ("POST", "/api/v1/projects/1/runs/R20260913000001/stop"),
    ],
)
async def test_scoped_run_endpoints_require_token(method: str, path: str) -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/runs"),
        ("GET", "/api/v1/runs/R20260913000001"),
        ("POST", "/api/v1/runs"),
        ("POST", "/api/v1/runs/R20260913000001/stop"),
    ],
)
async def test_legacy_flat_run_paths_removed(method: str, path: str) -> None:
    # 旧扁平路径已下线：无路由匹配 → 404
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 404


async def test_create_run_missing_scenario_id_rejected() -> None:
    # scenario_id 必填：422 在进入 handler（项目/场景校验）前失败，不会访问数据库
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.post(
            "/api/v1/projects/1/runs",
            json={},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_runs_invalid_project_id_type_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/not-an-int/runs",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_runs_invalid_page_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/1/runs?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


# ---------- 运行记录详情（落库，复用 conftest client/db_session） ----------


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _seed_run(db_session, project_id: int, run_no: str) -> None:
    scenario = Scenario(project_id=project_id, name=f"场景-{run_no}")
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(ScenarioRun(run_no=run_no, scenario_id=scenario.id))
    await db_session.commit()


async def test_get_run_detail_returns_same_as_list_item(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100001")

    r = await client.get(
        f"/api/v1/projects/{pid}/runs/R20260913100001", headers=_auth("alice")
    )
    assert r.status_code == 200
    detail = r.json()["data"]

    r = await client.get(f"/api/v1/projects/{pid}/runs", headers=_auth("alice"))
    items = r.json()["data"]["items"]
    assert detail in items
    assert detail["run_no"] == "R20260913100001"


async def test_get_run_detail_member_viewer_allowed(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100002")
    db_session.add(
        ProjectMember(
            project_id=pid, username="bob", role="viewer", granted_by="system"
        )
    )
    await db_session.commit()

    r = await client.get(
        f"/api/v1/projects/{pid}/runs/R20260913100002", headers=_auth("bob")
    )
    assert r.status_code == 200
    assert r.json()["data"]["run_no"] == "R20260913100002"


async def test_get_run_detail_non_member_rejected_3030(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100003")

    r = await client.get(
        f"/api/v1/projects/{pid}/runs/R20260913100003", headers=_auth("eve")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_get_run_detail_missing_rejected_2003(client) -> None:
    pid = await _create_project(client, "项目A")

    r = await client.get(
        f"/api/v1/projects/{pid}/runs/R-MISSING", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003


async def test_get_run_detail_cross_project_rejected_3022(client, db_session) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    await _seed_run(db_session, pid1, "R20260913100004")

    # 记录属于项目 A，从项目 B 访问 → 3022
    r = await client.get(
        f"/api/v1/projects/{pid2}/runs/R20260913100004", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3022
