"""测试方案契约测试：鉴权门禁 + 请求体校验（不落库）+ CRUD/挂载/预检/执行/级联。

覆盖：
- schema strip/422（不落库）
- 401 鉴权、422 查询参数、3030 非成员、3031 角色不足、admin 直通
- 3070 名称项目内重复、3071 不存在、3072 跨项目
- 3073 挂载场景不存在/跨项目、3074 方案内重复挂载、3076 空方案执行
- CRUD 全字段往返、分页/过滤、挂载装配（scenario_name + seq 排序）、全量替换挂载
- 删除预检/删除（owner+，仅清理关联行不触碰场景）
- execute 一键批量执行（monkeypatch orchestrator.create_run，成功/部分失败明细）
- 场景侧联动：被挂载场景严格删除 3017 阻断、force 解绑（场景保留方案、方案保留）
- 项目级联：严格 3023 提示含方案计数 + force 级联清理（关联行先删，场景保留）
"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.main import app
from app.models.project_member import ProjectMember
from app.models.scenario import Scenario
from app.models.test_plan import TestPlan
from app.models.test_plan_scenario import TestPlanScenario
from app.schemas import TestPlanIn, TestPlanUpdateIn
from app.services import orchestrator
from app.services.exceptions import BusinessError

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _create_plan(
    client, project_id: int, payload: dict, username: str = "alice"
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_id}/test-plans",
        json=payload,
        headers=_auth(username),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _mk_scenario(db_session, project_id: int, name: str) -> int:
    """直接落库场景行（方案挂载仅需场景存在且归属项目，不经场景 API）。"""
    sc = Scenario(project_id=project_id, name=name)
    db_session.add(sc)
    await db_session.commit()
    return sc.id


# ---------- schema 单元测试 ----------


def test_plan_in_strips_name() -> None:
    payload = TestPlanIn(name="  核心回归方案  ")
    assert payload.name == "核心回归方案"
    assert payload.report_template == "default"
    assert payload.pass_criteria == {}
    assert payload.scenarios == []


def test_plan_in_blank_name_raises() -> None:
    with pytest.raises(ValidationError):
        TestPlanIn(name="   ")


def test_plan_update_in_requires_one_field() -> None:
    with pytest.raises(ValidationError):
        TestPlanUpdateIn()
    # 仅传空挂载列表也是合法更新（全量替换为空）
    payload = TestPlanUpdateIn(scenarios=[])
    assert payload.scenarios == []


def test_plan_scenario_in_weight_seq_validation() -> None:
    with pytest.raises(ValidationError):
        TestPlanIn(name="p", scenarios=[{"scenario_id": 1, "seq": -1}])
    with pytest.raises(ValidationError):
        TestPlanIn(name="p", scenarios=[{"scenario_id": 1, "weight": 0}])


# ---------- 鉴权 / 422（不落库） ----------


async def test_create_plan_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/projects/1/test-plans", json={"name": "p"})
    assert resp.status_code == 401


async def test_list_plans_requires_token() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/projects/1/test-plans")
    assert resp.status_code == 401


async def test_create_plan_missing_name_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/projects/1/test-plans",
            json={},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_plans_invalid_page_422() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/projects/1/test-plans?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


# ---------- 权限门禁 ----------


async def test_create_plan_non_member_3030(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={"name": "p"},
        headers=_auth("eve"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3030


async def test_create_plan_viewer_3031(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="viewer", granted_by="x")
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={"name": "p"},
        headers=_auth("bob"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_create_plan_by_editor_success(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    data = await _create_plan(client, pid, {"name": "回归方案"}, username="bob")
    assert data["name"] == "回归方案"
    assert data["project_id"] == pid


async def test_admin_create_plan_without_membership(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={"name": "管理员建的方案"},
        headers=_auth("root", "admin"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "管理员建的方案"


# ---------- 创建 / 唯一性 / 挂载校验 ----------


async def test_create_plan_full_roundtrip(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    s1 = await _mk_scenario(db_session, pid, "登录场景")
    s2 = await _mk_scenario(db_session, pid, "下单场景")
    payload = {
        "name": "  核心回归方案  ",
        "pass_criteria": {"max_p95_ms": 500, "max_error_rate": 1.0},
        "report_template": "weekly",
        "description": "核心链路回归",
        "scenarios": [
            {"scenario_id": s2, "seq": 2, "weight": 2},
            {"scenario_id": s1, "seq": 1, "weight": 1},
        ],
    }
    data = await _create_plan(client, pid, payload)
    assert data["name"] == "核心回归方案"  # schema strip
    assert data["pass_criteria"] == {"max_p95_ms": 500, "max_error_rate": 1.0}
    assert data["report_template"] == "weekly"
    assert data["description"] == "核心链路回归"
    # 挂载按 seq 升序装配，scenario_name 已填充
    assert [(s["scenario_id"], s["seq"]) for s in data["scenarios"]] == [
        (s1, 1),
        (s2, 2),
    ]
    assert data["scenarios"][0]["scenario_name"] == "登录场景"
    assert data["scenarios"][1]["weight"] == 2
    assert "created_at" in data and data["id"] > 0


async def test_duplicate_plan_name_3070(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_plan(client, pid, {"name": "回归方案"})
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={"name": "回归方案"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3070


async def test_same_plan_name_allowed_across_projects(client) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    await _create_plan(client, pid1, {"name": "回归方案"})
    data = await _create_plan(client, pid2, {"name": "回归方案"})
    assert data["project_id"] == pid2


async def test_create_plan_scenario_not_exist_3073(client) -> None:
    pid = await _create_project(client, "项目A")
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={"name": "p", "scenarios": [{"scenario_id": 999999}]},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3073


async def test_create_plan_scenario_cross_project_3073(client, db_session) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    sid = await _mk_scenario(db_session, pid2, "他项目场景")
    r = await client.post(
        f"/api/v1/projects/{pid1}/test-plans",
        json={"name": "p", "scenarios": [{"scenario_id": sid}]},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3073


async def test_create_plan_duplicate_scenario_3074(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans",
        json={
            "name": "p",
            "scenarios": [
                {"scenario_id": sid, "seq": 1},
                {"scenario_id": sid, "seq": 2},
            ],
        },
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3074


# ---------- 列表 / 详情 ----------


async def test_list_plans_pagination_and_filters(client) -> None:
    pid = await _create_project(client, "项目A")
    await _create_plan(client, pid, {"name": "日常回归"})
    await _create_plan(client, pid, {"name": "发版回归"})
    await _create_plan(client, pid, {"name": "容量评估"})

    # id 倒序：最新创建的在前
    r = await client.get(f"/api/v1/projects/{pid}/test-plans", headers=_auth("alice"))
    body = r.json()["data"]
    assert body["total"] == 3
    assert [i["name"] for i in body["items"]] == ["容量评估", "发版回归", "日常回归"]

    # 名称模糊
    r = await client.get(
        f"/api/v1/projects/{pid}/test-plans?name=回归", headers=_auth("alice")
    )
    assert r.json()["data"]["total"] == 2

    # 分页
    r = await client.get(
        f"/api/v1/projects/{pid}/test-plans?page=1&page_size=2", headers=_auth("alice")
    )
    body = r.json()["data"]
    assert body["total"] == 3 and len(body["items"]) == 2


async def test_get_plan_detail_and_scope_errors(client, db_session) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    sid = await _mk_scenario(db_session, pid1, "登录场景")
    plan = await _create_plan(
        client, pid1, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )

    r = await client.get(
        f"/api/v1/projects/{pid1}/test-plans/{plan['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["scenarios"][0]["scenario_name"] == "登录场景"

    # 不存在
    r = await client.get(
        f"/api/v1/projects/{pid1}/test-plans/999999", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3071

    # 跨项目访问
    r = await client.get(
        f"/api/v1/projects/{pid2}/test-plans/{plan['id']}", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3072


# ---------- 更新 ----------


async def test_update_plan_success(client) -> None:
    pid = await _create_project(client, "项目A")
    plan = await _create_plan(
        client, pid, {"name": "回归方案", "pass_criteria": {"max_p95_ms": 500}}
    )
    r = await client.put(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}",
        json={"name": "  核心回归方案  ", "pass_criteria": {"max_p95_ms": 300}},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "核心回归方案"
    assert data["pass_criteria"] == {"max_p95_ms": 300}
    # 未传字段保持不变
    assert data["report_template"] == "default"


async def test_update_plan_empty_body_422(client) -> None:
    pid = await _create_project(client, "项目A")
    plan = await _create_plan(client, pid, {"name": "回归方案"})
    r = await client.put(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}",
        json={},
        headers=_auth("alice"),
    )
    assert r.status_code == 422


async def test_update_plan_duplicate_name_3070(client) -> None:
    pid = await _create_project(client, "项目A")
    p1 = await _create_plan(client, pid, {"name": "方案一"})
    await _create_plan(client, pid, {"name": "方案二"})
    r = await client.put(
        f"/api/v1/projects/{pid}/test-plans/{p1['id']}",
        json={"name": "方案二"},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3070


async def test_update_plan_replace_scenarios(client, db_session) -> None:
    """全量替换挂载：回读响应与 DB 一致（异步会话身份映射陷阱回归）。"""
    pid = await _create_project(client, "项目A")
    s1 = await _mk_scenario(db_session, pid, "登录场景")
    s2 = await _mk_scenario(db_session, pid, "下单场景")
    s3 = await _mk_scenario(db_session, pid, "支付场景")
    plan = await _create_plan(
        client,
        pid,
        {"name": "回归方案", "scenarios": [{"scenario_id": s1}, {"scenario_id": s2}]},
    )

    r = await client.put(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}",
        json={"scenarios": [{"scenario_id": s3, "seq": 5, "weight": 3}]},
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    scenarios = r.json()["data"]["scenarios"]
    assert [(s["scenario_id"], s["seq"], s["weight"]) for s in scenarios] == [
        (s3, 5, 3)
    ]

    # DB 层旧关联行已删除（delete-orphan 生效）
    rows = (
        (
            await db_session.execute(
                select(TestPlanScenario).where(TestPlanScenario.plan_id == plan["id"])
            )
        )
        .scalars()
        .all()
    )
    assert [(row.scenario_id, row.seq) for row in rows] == [(s3, 5)]


async def test_update_plan_scenario_scope_error_3073(client, db_session) -> None:
    pid1 = await _create_project(client, "项目A")
    pid2 = await _create_project(client, "项目B")
    sid = await _mk_scenario(db_session, pid2, "他项目场景")
    plan = await _create_plan(client, pid1, {"name": "回归方案"})
    r = await client.put(
        f"/api/v1/projects/{pid1}/test-plans/{plan['id']}",
        json={"scenarios": [{"scenario_id": sid}]},
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3073


# ---------- 删除预检 / 删除 ----------


async def test_precheck_plan_delete(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    plan = await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )
    r = await client.get(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}/delete-precheck",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"] == {
        "plan_id": plan["id"],
        "scenarios": 1,
        "references": 0,
    }


async def test_delete_plan_requires_owner(client, db_session) -> None:
    pid = await _create_project(client, "项目A")
    plan = await _create_plan(client, pid, {"name": "回归方案"})
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()
    r = await client.delete(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}", headers=_auth("bob")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3031


async def test_delete_plan_success_keeps_scenarios(client, db_session) -> None:
    """删除方案仅清理挂载关联行，场景本身保留（弱关联核心约束）。"""
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    plan = await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )
    r = await client.delete(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["deleted"] is True and data["removed_plan_scenarios"] == 1

    # 方案与关联行已删
    assert await db_session.get(TestPlan, plan["id"]) is None
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(TestPlanScenario)
            .where(TestPlanScenario.plan_id == plan["id"])
        )
    ).scalar()
    assert int(count) == 0
    # 场景保留
    assert await db_session.get(Scenario, sid) is not None


# ---------- 一键批量执行 ----------


async def test_execute_plan_success(client, db_session, monkeypatch) -> None:
    pid = await _create_project(client, "项目A")
    s1 = await _mk_scenario(db_session, pid, "登录场景")
    s2 = await _mk_scenario(db_session, pid, "下单场景")
    plan = await _create_plan(
        client,
        pid,
        {
            "name": "回归方案",
            "scenarios": [
                {"scenario_id": s2, "seq": 2},
                {"scenario_id": s1, "seq": 1},
            ],
        },
    )

    calls: list[int] = []

    async def fake_create_run(scenario_id, trigger, agent_ids=None, created_by=""):
        calls.append(scenario_id)
        return {"run_no": f"r-test-{scenario_id}", "agent_ids": ["a1"]}

    monkeypatch.setattr(orchestrator, "create_run", fake_create_run)

    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}/execute",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # 按 seq 升序触发：s1 先于 s2
    assert calls == [s1, s2]
    assert data["total"] == 2 and data["succeeded"] == 2 and data["failed"] == 0
    assert data["runs"][0]["run_no"] == f"r-test-{s1}"
    assert data["runs"][0]["scenario_name"] == "登录场景"
    assert data["runs"][1]["ok"] is True


async def test_execute_plan_partial_failure(client, db_session, monkeypatch) -> None:
    """单场景失败不阻断其余场景，返回逐场景明细。"""
    pid = await _create_project(client, "项目A")
    s1 = await _mk_scenario(db_session, pid, "登录场景")
    s2 = await _mk_scenario(db_session, pid, "下单场景")
    plan = await _create_plan(
        client,
        pid,
        {
            "name": "回归方案",
            "scenarios": [{"scenario_id": s1, "seq": 1}, {"scenario_id": s2, "seq": 2}],
        },
    )

    async def fake_create_run(scenario_id, trigger, agent_ids=None, created_by=""):
        if scenario_id == s1:
            raise BusinessError("无可用压力机，请先上线 Agent", code=2002)
        return {"run_no": f"r-test-{scenario_id}", "agent_ids": ["a1"]}

    monkeypatch.setattr(orchestrator, "create_run", fake_create_run)

    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}/execute",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["succeeded"] == 1 and data["failed"] == 1
    assert data["runs"][0]["ok"] is False
    assert "[2002]" in data["runs"][0]["error"]
    assert data["runs"][0]["run_no"] is None
    assert data["runs"][1]["ok"] is True


async def test_execute_plan_empty_3076(client) -> None:
    pid = await _create_project(client, "项目A")
    plan = await _create_plan(client, pid, {"name": "空方案"})
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}/execute",
        headers=_auth("alice"),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3076


async def test_execute_plan_editor_allowed(client, db_session, monkeypatch) -> None:
    """执行门禁与 create_run 一致：editor+ 可触发。"""
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    plan = await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )
    db_session.add(
        ProjectMember(project_id=pid, username="bob", role="editor", granted_by="x")
    )
    await db_session.commit()

    async def fake_create_run(scenario_id, trigger, agent_ids=None, created_by=""):
        assert created_by == "bob"
        return {"run_no": "r-test-1", "agent_ids": ["a1"]}

    monkeypatch.setattr(orchestrator, "create_run", fake_create_run)
    r = await client.post(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}/execute",
        headers=_auth("bob"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["succeeded"] == 1


# ---------- 场景侧联动（A4 弱关联约束） ----------


async def test_delete_scenario_blocked_by_plan_3017(client, db_session) -> None:
    """被方案挂载的场景严格模式拒绝删除（3017），预检含方案引用。"""
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )

    r = await client.get(
        f"/api/v1/projects/{pid}/scenarios/{sid}/delete-precheck",
        headers=_auth("alice"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["test_plans"][0]["name"] == "回归方案"

    r = await client.delete(
        f"/api/v1/projects/{pid}/scenarios/{sid}", headers=_auth("alice")
    )
    assert r.status_code == 400
    assert r.json()["code"] == 3017
    assert "回归方案" in r.json()["message"]


async def test_delete_scenario_force_unbinds_plan(client, db_session) -> None:
    """force 删除场景：解绑挂载关联行，方案本身保留。"""
    pid = await _create_project(client, "项目A")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    plan = await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )

    r = await client.delete(
        f"/api/v1/projects/{pid}/scenarios/{sid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_plan_refs"] == 1

    # 方案保留但挂载已清空
    r = await client.get(
        f"/api/v1/projects/{pid}/test-plans/{plan['id']}", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["scenarios"] == []


# ---------- 项目级联 ----------


async def test_project_strict_block_message_includes_plans(client) -> None:
    """严格模式 3023 提示应包含方案计数；预检接口返回 test_plans 计数。"""
    pid = await _create_project(client, "级联项目")
    await _create_plan(client, pid, {"name": "回归方案"})

    r = await client.delete(f"/api/v1/projects/{pid}", headers=_auth("alice"))
    assert r.status_code == 400
    assert r.json()["code"] == 3023
    assert "1 个测试方案" in r.json()["message"]

    r = await client.get(
        f"/api/v1/projects/{pid}/delete-precheck", headers=_auth("alice")
    )
    assert r.json()["data"]["test_plans"] == 1


async def test_project_force_delete_cascades_plans(client, db_session) -> None:
    """项目 force 删除：先解绑挂载关联行再删方案（FK RESTRICT 顺序约束）。"""
    pid = await _create_project(client, "级联项目")
    sid = await _mk_scenario(db_session, pid, "登录场景")
    await _create_plan(
        client, pid, {"name": "回归方案", "scenarios": [{"scenario_id": sid}]}
    )

    r = await client.delete(
        f"/api/v1/projects/{pid}?force=true", headers=_auth("alice")
    )
    assert r.status_code == 200
    assert r.json()["data"]["removed_test_plans"] == 1

    for model in (TestPlan, TestPlanScenario):
        count = (
            await db_session.execute(select(func.count()).select_from(model))
        ).scalar()
        assert int(count) == 0
