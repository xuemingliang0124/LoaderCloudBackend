"""P2 权限收口契约测试：项目成员授权（3030/3031）+ admin 直通 + run 可见性。

基于 conftest 的 aiosqlite 内存库，覆盖真实授权路径；
metrics/WS 共用的 ensure_run_visible 在此一并验证。
"""

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.services import es_client


def _auth(username: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str) -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username, "viewer")
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _add_member(db_session, project_id: int, username: str, role: str) -> None:
    db_session.add(
        ProjectMember(
            project_id=project_id, username=username, role=role, granted_by="system"
        )
    )
    await db_session.commit()


# ---------- 建项目自动 owner ----------


async def test_create_project_grants_owner(client, db_session) -> None:
    pid = await _create_project(client, "项目A", "alice")
    rows = (await db_session.execute(select(ProjectMember))).scalars().all()
    assert len(rows) == 1
    assert rows[0].project_id == pid
    assert rows[0].username == "alice"
    assert rows[0].role == "owner"


# ---------- 项目列表按成员过滤 ----------


async def test_project_list_filters_by_membership(client) -> None:
    await _create_project(client, "项目A", "alice")

    # 非成员 bob：看不到
    r = await client.get("/api/v1/projects", headers=_auth("bob", "viewer"))
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 0

    # 创建者 alice：可见
    r = await client.get("/api/v1/projects", headers=_auth("alice", "viewer"))
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1

    # admin：全量可见（不依赖成员关系）
    r = await client.get("/api/v1/projects", headers=_auth("root", "admin"))
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1


# ---------- 资源访问门禁：3030 / 3021 优先级 / 3031 ----------


async def test_non_member_rejected_3030(client) -> None:
    pid = await _create_project(client, "项目A", "alice")
    r = await client.get(
        f"/api/v1/projects/{pid}/scripts", headers=_auth("bob", "viewer")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_3021_precedes_3030_for_missing_project(client) -> None:
    await _create_project(client, "项目A", "alice")
    r = await client.get(
        "/api/v1/projects/99999/scripts", headers=_auth("bob", "viewer")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3021


async def test_admin_bypasses_membership(client) -> None:
    pid = await _create_project(client, "项目A", "alice")
    r = await client.get(
        f"/api/v1/projects/{pid}/scripts", headers=_auth("root", "admin")
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"total": 0, "items": []}


async def test_viewer_cannot_write_3031_but_owner_can_proceed(
    client, db_session
) -> None:
    pid = await _create_project(client, "项目A", "alice")
    await _add_member(db_session, pid, "bob", "viewer")
    body = {"name": "job1", "scenario_id": 9999, "cron": "*/5 * * * *"}

    # viewer 调 editor 接口 → 3031
    r = await client.post(
        f"/api/v1/projects/{pid}/schedules", json=body, headers=_auth("bob", "viewer")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031

    # owner 达到 editor 级 → 通过门禁，走到后续场景校验（3013：场景不存在）
    r = await client.post(
        f"/api/v1/projects/{pid}/schedules", json=body, headers=_auth("alice", "viewer")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3013


# ---------- 正向链路：editor 建定时任务，viewer 可读 ----------


async def test_editor_creates_schedule_viewer_reads(client, db_session) -> None:
    pid = await _create_project(client, "项目A", "alice")
    scenario = Scenario(project_id=pid, name="场景1")
    db_session.add(scenario)
    await db_session.commit()

    body = {"name": "job1", "scenario_id": scenario.id, "cron": "*/5 * * * *"}
    r = await client.post(
        f"/api/v1/projects/{pid}/schedules", json=body, headers=_auth("alice", "viewer")
    )
    assert r.status_code == 200

    await _add_member(db_session, pid, "bob", "viewer")
    r = await client.get(
        f"/api/v1/projects/{pid}/schedules", headers=_auth("bob", "viewer")
    )
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1


# ---------- run 可见性：metrics 与 WS 共用 ensure_run_visible ----------


async def _seed_run(db_session, project_id: int, run_no: str) -> None:
    scenario = Scenario(project_id=project_id, name=f"场景-{run_no}")
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(ScenarioRun(run_no=run_no, scenario_id=scenario.id))
    await db_session.commit()


async def test_metrics_run_visibility(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A", "alice")
    await _add_member(db_session, pid, "bob", "viewer")
    await _seed_run(db_session, pid, "R20260913001")
    url = "/api/v1/metrics/timeseries?run_no=R20260913001&start=0&end=10"

    # 非成员 → 3030（不泄露执行记录存在性）
    r = await client.get(url, headers=_auth("eve", "viewer"))
    assert r.status_code == 400
    assert r.json()["code"] == 3030

    # 记录不存在 → 2003
    r = await client.get(
        "/api/v1/metrics/timeseries?run_no=missing&start=0&end=10",
        headers=_auth("alice", "viewer"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 2003

    # 成员 viewer → 放行（ES 查询 mock 掉）
    async def _fake_timeseries(run_no, start, end, interval):
        return {"series": []}

    monkeypatch.setattr(es_client, "query_timeseries", _fake_timeseries)
    r = await client.get(url, headers=_auth("bob", "viewer"))
    assert r.status_code == 200
    assert r.json()["data"] == {"series": []}


# ---------- WS 握手门禁 ----------


def test_ws_rejects_invalid_token_without_db() -> None:
    """非法 token：1008 拒绝且不触库（SessionLocal 未被调用即可跑无 DB 环境）。

    授权拒绝（非成员/记录不存在）同样走 1008，其判定逻辑与 metrics 共用
    ensure_run_visible，已在上方用例充分覆盖。
    """
    from starlette.testclient import TestClient

    from app.main import app

    tc = TestClient(app)  # 不进 lifespan（避免触发真实 MySQL/ES/MinIO 初始化）
    with pytest.raises(WebSocketDisconnect):
        with tc.websocket_connect("/ws/runs/R1?token=bad") as ws:
            ws.receive_json()
