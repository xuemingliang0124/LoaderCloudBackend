"""项目管理契约测试：鉴权门禁 + 请求体校验（不落库）+ 更新/删除（aiosqlite 落库）。"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.api.v1.projects import _role_cn
from app.main import app
from app.models.enums import RunStatus, ScenarioType
from app.models.project_member import ProjectMember
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.schedule import ScheduleJob
from app.models.script import Script
from app.schemas import ProjectIn

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


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


# ---------- 更新（PUT /projects/{id}） ----------


async def test_update_project_requires_token(client) -> None:
    r = await client.put("/api/v1/projects/1", json={"name": "新名"})
    assert r.status_code == 401


async def test_update_project_validation_422(client) -> None:
    # 空更新体 / 纯空白名称：422，校验阶段失败不触库
    r = await client.put("/api/v1/projects/1", json={}, headers=_auth("alice"))
    assert r.status_code == 422
    r = await client.put(
        "/api/v1/projects/1", json={"name": "   "}, headers=_auth("alice")
    )
    assert r.status_code == 422


async def test_update_project_non_member_3030(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.put(
        f"/api/v1/projects/{pid}", json={"name": "改名"}, headers=_auth("eve")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_update_project_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.put(
        f"/api/v1/projects/{pid}", json={"name": "改名"}, headers=_auth("bob")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_update_project_success_by_owner(client, db_session) -> None:
    pid = await _create_project(client, "旧名")
    r = await client.put(
        f"/api/v1/projects/{pid}",
        json={"name": "  新名  ", "description": "新描述"},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "新名"  # schema 去除首尾空白
    assert data["description"] == "新描述"


async def test_update_project_duplicate_name_3020(client) -> None:
    await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    r = await client.put(
        f"/api/v1/projects/{pid2}", json={"name": "项目A"}, headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3020


async def test_admin_can_update_without_membership(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.put(
        f"/api/v1/projects/{pid}",
        json={"description": "管理员改的"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["description"] == "管理员改的"


# ---------- my_role 字段 ----------


async def test_create_project_returns_my_role_owner(client) -> None:
    r = await client.post(
        "/api/v1/projects", json={"name": "角色项目"}, headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["my_role"] == "项目管理员"


async def test_list_projects_returns_my_role_per_member(client, db_session) -> None:
    pid = await _create_project(client, "角色A")
    db_session.add_all(
        [
            ProjectMember(
                project_id=pid, username="bob", role="editor", granted_by="x"
            ),
            ProjectMember(
                project_id=pid, username="carol", role="viewer", granted_by="x"
            ),
        ]
    )
    await db_session.commit()

    r = await client.get("/api/v1/projects", headers=_auth("alice"))
    assert r.json()["data"]["items"][0]["my_role"] == "项目管理员"

    r = await client.get("/api/v1/projects", headers=_auth("bob"))
    assert r.json()["data"]["items"][0]["my_role"] == "编辑者"

    r = await client.get("/api/v1/projects", headers=_auth("carol"))
    assert r.json()["data"]["items"][0]["my_role"] == "观察者"

    # admin 非成员：具备管理员级能力，恒出「项目管理员」
    r = await client.get("/api/v1/projects", headers=_auth("root", "admin"))
    roles = {i["name"]: i["my_role"] for i in r.json()["data"]["items"]}
    assert roles["角色A"] == "项目管理员"


async def test_update_project_returns_my_role_owner(client) -> None:
    pid = await _create_project(client, "角色B")
    r = await client.put(
        f"/api/v1/projects/{pid}", json={"name": "角色B2"}, headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["my_role"] == "项目管理员"


# ---------- my_role 补充单元测试 ----------


def test_role_cn_helper_mapping() -> None:
    """_role_cn：DB 英文角色 → API 中文角色名映射。"""
    assert _role_cn("owner") == "项目管理员"
    assert _role_cn("OWNER") == "项目管理员"
    assert _role_cn("editor") == "编辑者"
    assert _role_cn("viewer") == "观察者"


def test_project_out_my_role_default_blank() -> None:
    """schema 层：ORM 直构时 my_role 默认空串（由接口按调用者填充）。"""
    from datetime import datetime

    from app.models.project import Project
    from app.schemas import ProjectOut

    now = datetime.now()
    project = Project(
        id=1,
        name="p",
        description="",
        created_by="alice",
        created_at=now,
        updated_at=now,
    )
    out = ProjectOut.model_validate(project)
    assert out.my_role == ""


async def test_create_project_by_admin_returns_my_role_owner(client) -> None:
    """admin 建项目同样写入 owner 成员行，my_role 为项目管理员。"""
    r = await client.post(
        "/api/v1/projects", json={"name": "管理员建"}, headers=_auth("root", "admin")
    )
    assert r.status_code == 200
    assert r.json()["data"]["my_role"] == "项目管理员"


async def test_list_my_role_follows_pagination(client, db_session) -> None:
    """bob 在两个项目角色不同：分页后每行 my_role 与该项目内角色一一对应不串位。"""
    pid1 = await _create_project(client, "页一")
    pid2 = await _create_project(client, "页二")
    db_session.add_all(
        [
            ProjectMember(
                project_id=pid1, username="bob", role="editor", granted_by="x"
            ),
            ProjectMember(
                project_id=pid2, username="bob", role="viewer", granted_by="x"
            ),
        ]
    )
    await db_session.commit()

    # id 倒序：page 1 → 页二(viewer)，page 2 → 页一(editor)
    r = await client.get("/api/v1/projects?page=1&page_size=1", headers=_auth("bob"))
    items = r.json()["data"]["items"]
    assert [(i["name"], i["my_role"]) for i in items] == [("页二", "观察者")]

    r = await client.get("/api/v1/projects?page=2&page_size=1", headers=_auth("bob"))
    items = r.json()["data"]["items"]
    assert [(i["name"], i["my_role"]) for i in items] == [("页一", "编辑者")]


async def test_list_my_role_with_fuzzy_filter(client, db_session) -> None:
    """名称模糊过滤后，剩余行的 my_role 仍按成员角色正确映射。"""
    pid1 = await _create_project(client, "压测一")
    await _create_project(client, "压测二")
    db_session.add(
        ProjectMember(project_id=pid1, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()

    r = await client.get("/api/v1/projects?name=压测一", headers=_auth("bob"))
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["name"] == "压测一"
    assert items[0]["my_role"] == "观察者"


async def test_admin_member_still_sees_owner_capability(client, db_session) -> None:
    """admin 即使成员角色是 viewer，my_role 仍出「项目管理员」（能力语义覆盖成员语义）。"""
    pid = await _create_project(client, "角色C")
    db_session.add(
        ProjectMember(project_id=pid, username="root", role="viewer", granted_by="x")
    )
    await db_session.commit()

    r = await client.get("/api/v1/projects", headers=_auth("root", "admin"))
    roles = {i["name"]: i["my_role"] for i in r.json()["data"]["items"]}
    assert roles["角色C"] == "项目管理员"


# ---------- 删除预检 / 删除 ----------


async def test_delete_project_requires_token(client) -> None:
    r = await client.delete("/api/v1/projects/1")
    assert r.status_code == 401


async def test_delete_project_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("bob"))
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_precheck_counts(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    script = Script(project_id=pid, name="s1", created_by="alice")
    scenario = Scenario(project_id=pid, name="sc1", scenario_type=ScenarioType.MIXED)
    db_session.add_all([script, scenario])
    await db_session.flush()
    db_session.add_all(
        [
            ScheduleJob(name="j1", scenario_id=scenario.id, cron="* * * * *"),
            ScenarioRun(
                run_no="R-PRE", scenario_id=scenario.id, status=RunStatus.RUNNING
            ),
            ScenarioRun(
                run_no="R-DONE", scenario_id=scenario.id, status=RunStatus.FINISHED
            ),
        ]
    )
    await db_session.commit()

    r = await client.get(
        f"/api/v1/projects/{pid}/delete-precheck", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["scripts"] == 1
    assert data["scenarios"] == 1
    assert data["running_runs"] == 1
    assert data["schedule_jobs"] == [
        {"id": data["schedule_jobs"][0]["id"], "name": "j1"}
    ]


async def test_delete_project_strict_blocked_3023(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(Script(project_id=pid, name="s1", created_by="alice"))
    await db_session.commit()
    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 3023
    # 提示语中包含资产计数
    assert "1 个脚本" in r.json()["message"]


async def test_delete_project_running_blocks_force_3014(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    scenario = Scenario(project_id=pid, name="sc1")
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(
        ScenarioRun(run_no="R-RUN", scenario_id=scenario.id, status=RunStatus.RUNNING)
    )
    await db_session.commit()

    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3014


async def test_delete_empty_project_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()

    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["deleted"] is True
    assert data["force"] is False

    members = (
        await db_session.execute(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == pid)
        )
    ).scalar()
    assert int(members) == 0
    from app.models.project import Project

    assert (
        await db_session.execute(select(Project).where(Project.id == pid))
    ).scalar_one_or_none() is None


async def test_force_cascade_deletes_all_assets(client, db_session) -> None:
    pid = await _create_project(client, "全量项目")
    script = Script(project_id=pid, name="s1", file_key="scripts/x/v1/a.jmx")
    scenario = Scenario(project_id=pid, name="sc1", scenario_type=ScenarioType.MIXED)
    db_session.add_all([script, scenario])
    await db_session.flush()
    ss = ScenarioScript(
        scenario_id=scenario.id,
        script_id=script.id,
        order_index=0,
        agent_tags=[],
        agent_count=1,
    )
    db_session.add(ss)
    await db_session.flush()
    db_session.add_all(
        [
            ScenarioScriptTG(
                scenario_script_id=ss.id,
                thread_group_name="tg1",
                testclass="ThreadGroup",
                num_threads=10,
                ramp_time=0,
                tps=0,
                scheduler=False,
                duration=0,
            ),
            ScheduleJob(name="j1", scenario_id=scenario.id, cron="* * * * *"),
            ScenarioRun(
                run_no="R-F1", scenario_id=scenario.id, status=RunStatus.FINISHED
            ),
        ]
    )
    await db_session.flush()
    db_session.add(
        RunAgentResult(run_no="R-F1", agent_id="a1", scenario_script_id=ss.id)
    )
    await db_session.commit()

    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data == {
        "id": pid,
        "deleted": True,
        "force": True,
        "removed_scripts": 1,
        "removed_scenarios": 1,
        "removed_runs": 1,
        "removed_schedules": 1,
        "removed_artifacts": 0,  # 测试环境无 MinIO，delete_prefix best-effort 返回 0
    }

    # 全部资产级联清理（每测试独立内存库，逐表计数应为 0）
    from app.models.project import Project
    from app.models.scenario_script_tg import ScenarioScriptTG as TG

    checks: list[tuple[str, object]] = [
        ("test_project", select(Project).where(Project.id == pid)),
        (
            "project_member",
            select(ProjectMember).where(ProjectMember.project_id == pid),
        ),
        ("test_scenario", select(Scenario).where(Scenario.project_id == pid)),
        ("jmeter_script", select(Script).where(Script.project_id == pid)),
        ("scenario_script", select(ScenarioScript)),
        ("scenario_script_tg", select(TG)),
        ("schedule_job", select(ScheduleJob)),
        ("scenario_run", select(ScenarioRun)),
        ("run_agent_result", select(RunAgentResult)),
    ]
    for table, stmt in checks:
        remaining = (await db_session.execute(stmt)).scalars().all()
        assert remaining == [], f"{table} 未清理"
