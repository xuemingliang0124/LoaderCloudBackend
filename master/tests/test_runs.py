"""执行管理契约测试：项目嵌套路由的鉴权门禁、参数校验与旧路径下线（不落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.main import app
from app.models.enums import RunStatus
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services import es_client

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
        ("GET", "/api/v1/runs/R20260913000001/summary"),
        ("GET", "/api/v1/runs/R20260913000001/realtime-summary"),
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


# ---------- 执行终态汇总（扁平路由 /runs/{run_no}/summary，ES mock） ----------


async def test_run_summary_missing_run_rejected_2003(client) -> None:
    r = await client.get(
        "/api/v1/runs/R-SUMMARY-MISSING/summary", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003


async def test_run_summary_non_member_rejected_3030(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100011")

    r = await client.get("/api/v1/runs/R20260913100011/summary", headers=_auth("eve"))
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_run_summary_running_returns_2004(client, db_session, monkeypatch):
    """执行中（默认 PENDING）无汇总文档 → 2004「尚未结束」。"""

    async def _fake_none(run_no: str) -> None:
        return None

    monkeypatch.setattr(es_client, "query_summary", _fake_none)
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100012")

    r = await client.get("/api/v1/runs/R20260913100012/summary", headers=_auth("alice"))
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == 2004
    assert "尚未结束" in body["message"]


async def test_run_summary_terminal_missing_doc_returns_2004(
    client, db_session, monkeypatch
):
    """已终态但汇总文档缺失（历史数据/ES 丢数）→ 2004「不存在」。"""

    async def _fake_none(run_no: str) -> None:
        return None

    monkeypatch.setattr(es_client, "query_summary", _fake_none)
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100013")
    run = (
        (
            await db_session.execute(
                select(ScenarioRun).where(ScenarioRun.run_no == "R20260913100013")
            )
        )
        .scalars()
        .first()
    )
    run.status = RunStatus.FINISHED
    await db_session.commit()

    r = await client.get("/api/v1/runs/R20260913100013/summary", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 2004


async def test_run_summary_passthrough_on_hit(client, db_session, monkeypatch):
    """命中汇总文档：原样透出（含 agents/failed_agents/summary/artifacts/stopped）。"""
    doc = {
        "agents": ["agent-1"],
        "failed_agents": [],
        "summary": {
            "samples": 3,
            "success": 2,
            "errors": 1,
            "min_rt": 100.0,
            "max_rt": 300.0,
            "avg_rt": 200.0,
            "p95_rt": 300.0,
            "avg_tps": 2.0,
            "failed": False,
            "by_label": [
                {
                    "label": "下单",
                    "sample_type": "request",
                    "samples": 3,
                    "success": 2,
                    "errors": 1,
                    "min_rt": 100.0,
                    "max_rt": 300.0,
                    "avg_rt": 200.0,
                    "p95_rt": 300.0,
                    "avg_tps": 2.0,
                }
            ],
        },
        "artifacts": [],
        "stopped": False,
    }

    async def _fake_hit(run_no: str) -> dict:
        assert run_no == "R20260913100014"
        return doc

    monkeypatch.setattr(es_client, "query_summary", _fake_hit)
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100014")

    r = await client.get("/api/v1/runs/R20260913100014/summary", headers=_auth("alice"))
    assert r.status_code == 200
    assert r.json()["data"] == doc


# ---------- 执行期实时汇总（扁平路由 /runs/{run_no}/realtime-summary，ES mock） ----------


async def test_realtime_summary_missing_run_rejected_2003(client) -> None:
    """run_no 不存在：ensure_run_visible 返回 2003。"""
    r = await client.get(
        "/api/v1/runs/R-REALTIME-MISSING/realtime-summary", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003


async def test_realtime_summary_non_member_rejected_3030(
    client, db_session
) -> None:
    """非项目成员：3030。"""
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100021")

    r = await client.get(
        "/api/v1/runs/R20260913100021/realtime-summary", headers=_auth("eve")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_realtime_summary_empty_returns_zeros(client, db_session, monkeypatch):
    """执行中无 metrics 文档：返回全零汇总（不抛 2004，与终态 /summary 区别）。"""

    async def _fake_empty(run_no: str) -> dict:
        return {
            "samples": 0,
            "success": 0,
            "errors": 0,
            "min_rt": 0.0,
            "max_rt": 0.0,
            "avg_rt": 0.0,
            "p95_rt": 0.0,
            "avg_tps": 0.0,
            "by_label": [],
        }

    monkeypatch.setattr(es_client, "query_realtime_summary", _fake_empty)
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100022")

    r = await client.get(
        "/api/v1/runs/R20260913100022/realtime-summary", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["samples"] == 0
    assert data["avg_tps"] == 0.0
    assert data["by_label"] == []


async def test_realtime_summary_passthrough_on_hit(client, db_session, monkeypatch):
    """命中 metrics：原样透出 ES 聚合结果。"""
    doc = {
        "samples": 100,
        "success": 95,
        "errors": 5,
        "min_rt": 50.0,
        "max_rt": 500.0,
        "avg_rt": 150.0,
        "p95_rt": 300.0,
        "avg_tps": 10.0,
        "by_label": [
            {
                "label": "下单",
                "sample_type": "request",
                "samples": 60,
                "success": 57,
                "errors": 3,
                "min_rt": 50.0,
                "max_rt": 400.0,
                "avg_rt": 120.0,
                "p95_rt": 250.0,
                "avg_tps": 6.0,
            },
            {
                "label": "查询",
                "sample_type": "request",
                "samples": 40,
                "success": 38,
                "errors": 2,
                "min_rt": 60.0,
                "max_rt": 500.0,
                "avg_rt": 200.0,
                "p95_rt": 300.0,
                "avg_tps": 4.0,
            },
        ],
    }

    async def _fake_hit(run_no: str) -> dict:
        assert run_no == "R20260913100023"
        return doc

    monkeypatch.setattr(es_client, "query_realtime_summary", _fake_hit)
    pid = await _create_project(client, "项目A")
    await _seed_run(db_session, pid, "R20260913100023")

    r = await client.get(
        "/api/v1/runs/R20260913100023/realtime-summary", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"] == doc
