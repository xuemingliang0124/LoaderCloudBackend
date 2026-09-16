"""A3 场景绑定环境测试：scenario.environment_id 闭环（CRUD/校验/解绑）+
orchestrator._build_jmeter_args 参数合并优先级单元测试。

覆盖：
- 场景创建/更新时 environment_id 归属校验（3041 不存在 / 3042 跨项目）
- 场景响应含 environment_id（绑定/未绑定/null 解绑）
- 环境删除预检返回引用场景数；严格 3043 + force 解绑再删
- orchestrator 执行期 -J 参数合并：环境变量为基础，场景 param_overrides 覆盖
"""

from app.core.security import create_access_token
from app.models.environment import Environment
from app.models.scenario import Scenario
from app.services.orchestrator import _build_jmeter_args

_TOKEN = create_access_token("tester", "viewer")


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


async def _create_env(
    client, project_id: int, env_code: str, variables: dict | None = None
) -> dict:
    payload = {"name": env_code, "env_code": env_code}
    if variables is not None:
        payload["variables"] = variables
    resp = await client.post(
        f"/api/v1/projects/{project_id}/environments",
        json=payload,
        headers=_auth("alice"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _create_script(client, db_session, project_id: int, name: str) -> int:
    from app.models.script import Script

    script = Script(project_id=project_id, name=name, file_key=f"scripts/x/v1/{name}.jmx")
    db_session.add(script)
    await db_session.commit()
    return script.id


# ---------- orchestrator._build_jmeter_args 单元测试 ----------


def _scenario_stub(param_overrides: dict | None, environment_id: int | None = None) -> Scenario:
    return Scenario(
        project_id=1,
        name="stub",
        param_overrides=param_overrides,
        environment_id=environment_id,
    )


def _env_stub(variables: dict | None) -> Environment:
    return Environment(
        project_id=1,
        name="stub",
        env_code="stub",
        variables=variables,
    )


def test_build_jmeter_args_no_env_no_overrides() -> None:
    """未绑定环境且无 param_overrides：返回空 dict。"""
    args = _build_jmeter_args(_scenario_stub(None), None)
    assert args == {}


def test_build_jmeter_args_only_env_variables() -> None:
    """仅绑定环境：环境 variables 全部注入。"""
    env = _env_stub({"base_url": "https://api.demo.com", "timeout": "30"})
    args = _build_jmeter_args(_scenario_stub(None), env)
    assert args == {"base_url": "https://api.demo.com", "timeout": "30"}


def test_build_jmeter_args_only_scenario_overrides() -> None:
    """未绑定环境但有 param_overrides：等价旧行为（向后兼容）。"""
    args = _build_jmeter_args(
        _scenario_stub({"host": "api.demo.com"}), None
    )
    assert args == {"host": "api.demo.com"}


def test_build_jmeter_args_scenario_overrides_env() -> None:
    """场景 param_overrides 覆盖环境 variables 同名键。"""
    env = _env_stub({"host": "env.demo.com", "port": "8080"})
    args = _build_jmeter_args(
        _scenario_stub({"host": "scenario.demo.com"}), env
    )
    assert args == {"host": "scenario.demo.com", "port": "8080"}


def test_build_jmeter_args_env_variables_str_coerced() -> None:
    """环境 variables 非字符串值统一转 str（JMeter -J 接受字符串）。"""
    env = _env_stub({"threads": 100, "ratio": 0.5, "flag": True})
    args = _build_jmeter_args(_scenario_stub(None), env)
    assert args == {"threads": "100", "ratio": "0.5", "flag": "True"}


def test_build_jmeter_args_env_empty_variables() -> None:
    """环境 variables 为空 dict：等价未绑定环境。"""
    env = _env_stub({})
    args = _build_jmeter_args(_scenario_stub({"host": "x"}), env)
    assert args == {"host": "x"}


# ---------- 场景 CRUD：environment_id 校验与持久化 ----------


async def test_create_scenario_with_environment_id(client, db_session) -> None:
    """创建场景绑定环境：响应含 environment_id。"""
    pid = await _create_project(client, "场景绑定环境项目")
    env = await _create_env(client, pid, "prod", {"base_url": "https://api.demo.com"})
    script_id = await _create_script(client, db_session, pid, "s1")
    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "绑定环境的场景",
            "scenario_type": "混合场景",
            "duration": 600,
            "environment_id": env["id"],
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["environment_id"] == env["id"]


async def test_create_scenario_without_environment_id_defaults_null(
    client, db_session
) -> None:
    """不传 environment_id：默认 None（向后兼容存量场景）。"""
    pid = await _create_project(client, "场景不绑定环境项目")
    script_id = await _create_script(client, db_session, pid, "s2")
    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "不绑定环境的场景",
            "scenario_type": "混合场景",
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["environment_id"] is None


async def test_create_scenario_with_null_environment_id(client, db_session) -> None:
    """显式传 environment_id=null：等价不绑定。"""
    pid = await _create_project(client, "场景null环境项目")
    script_id = await _create_script(client, db_session, pid, "s3")
    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "显式null环境场景",
            "scenario_type": "混合场景",
            "environment_id": None,
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["environment_id"] is None


async def test_create_scenario_with_nonexistent_environment_rejected_3041(
    client, db_session
) -> None:
    """environment_id 指向不存在的环境：3041。"""
    pid = await _create_project(client, "场景环境不存在项目")
    script_id = await _create_script(client, db_session, pid, "s4")
    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "环境不存在场景",
            "scenario_type": "混合场景",
            "environment_id": 999999,
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3041


async def test_create_scenario_with_cross_project_environment_rejected_3042(
    client, db_session
) -> None:
    """environment_id 指向其他项目的环境：3042。"""
    pid1 = await _create_project(client, "场景项目A")
    pid2 = await _create_project(client, "环境项目B")
    env_b = await _create_env(client, pid2, "prod_b")
    script_id = await _create_script(client, db_session, pid1, "s5")
    resp = await client.post(
        f"/api/v1/projects/{pid1}/scenarios",
        json={
            "name": "跨项目环境场景",
            "scenario_type": "混合场景",
            "environment_id": env_b["id"],
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3042


async def test_update_scenario_rebind_environment(client, db_session) -> None:
    """更新场景的 environment_id：解绑/重绑。"""
    pid = await _create_project(client, "场景重绑环境项目")
    env1 = await _create_env(client, pid, "dev", {"host": "dev.demo.com"})
    env2 = await _create_env(client, pid, "prod", {"host": "prod.demo.com"})
    script_id = await _create_script(client, db_session, pid, "s6")
    base = {
        "scenario_type": "混合场景",
        "duration": 600,
        "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
    }
    create = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={**base, "name": "重绑场景", "environment_id": env1["id"]},
        headers=_auth("alice"),
    )
    assert create.status_code == 200
    sid = create.json()["data"]["id"]

    # 重绑到 env2
    update = await client.put(
        f"/api/v1/projects/{pid}/scenarios/{sid}",
        json={**base, "name": "重绑场景", "environment_id": env2["id"]},
        headers=_auth("alice"),
    )
    assert update.status_code == 200
    assert update.json()["data"]["environment_id"] == env2["id"]

    # 解绑（null）
    unbind = await client.put(
        f"/api/v1/projects/{pid}/scenarios/{sid}",
        json={**base, "name": "重绑场景", "environment_id": None},
        headers=_auth("alice"),
    )
    assert unbind.status_code == 200
    assert unbind.json()["data"]["environment_id"] is None


async def test_update_scenario_environment_cross_project_rejected_3042(
    client, db_session
) -> None:
    """更新场景时绑定跨项目环境：3042。"""
    pid1 = await _create_project(client, "更新场景项目A")
    pid2 = await _create_project(client, "更新场景环境项目B")
    env_b = await _create_env(client, pid2, "prod_b")
    script_id = await _create_script(client, db_session, pid1, "s7")
    base = {
        "scenario_type": "混合场景",
        "duration": 600,
        "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
    }
    create = await client.post(
        f"/api/v1/projects/{pid1}/scenarios",
        json={**base, "name": "更新跨项目场景"},
        headers=_auth("alice"),
    )
    assert create.status_code == 200
    sid = create.json()["data"]["id"]
    update = await client.put(
        f"/api/v1/projects/{pid1}/scenarios/{sid}",
        json={**base, "name": "更新跨项目场景", "environment_id": env_b["id"]},
        headers=_auth("alice"),
    )
    assert update.status_code == 400
    assert update.json()["code"] == 3042


async def test_get_scenario_detail_includes_environment_id(client, db_session) -> None:
    """场景详情响应含 environment_id 字段。"""
    pid = await _create_project(client, "场景详情环境项目")
    env = await _create_env(client, pid, "prod", {"base_url": "https://x"})
    script_id = await _create_script(client, db_session, pid, "s8")
    create = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "详情环境场景",
            "scenario_type": "混合场景",
            "environment_id": env["id"],
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    assert create.status_code == 200
    sid = create.json()["data"]["id"]
    detail = await client.get(
        f"/api/v1/projects/{pid}/scenarios/{sid}", headers=_auth("alice")
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["environment_id"] == env["id"]


# ---------- 环境删除预检 + 严格 3043 + force 解绑 ----------


async def test_environment_delete_precheck_returns_scenario_count(
    client, db_session
) -> None:
    """环境删除预检：被场景引用时返回引用数。"""
    pid = await _create_project(client, "环境预检项目")
    env = await _create_env(client, pid, "prod")
    script_id = await _create_script(client, db_session, pid, "s9")
    # 创建 2 个场景引用同一环境
    for i in range(2):
        await client.post(
            f"/api/v1/projects/{pid}/scenarios",
            json={
                "name": f"预检场景{i}",
                "scenario_type": "混合场景",
                "environment_id": env["id"],
                "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
            },
            headers=_auth("alice"),
        )
    precheck = await client.get(
        f"/api/v1/projects/{pid}/environments/{env['id']}/delete-precheck",
        headers=_auth("alice"),
    )
    assert precheck.status_code == 200
    assert precheck.json()["data"]["scenarios"] == 2


async def test_environment_delete_precheck_zero_when_unbound(client, db_session) -> None:
    """环境删除预检：无场景引用时返回 0。"""
    pid = await _create_project(client, "环境无引用项目")
    env = await _create_env(client, pid, "dev")
    precheck = await client.get(
        f"/api/v1/projects/{pid}/environments/{env['id']}/delete-precheck",
        headers=_auth("alice"),
    )
    assert precheck.status_code == 200
    assert precheck.json()["data"]["scenarios"] == 0


async def test_environment_delete_strict_rejected_3043(client, db_session) -> None:
    """环境删除严格模式：被场景引用时拒绝 3043。"""
    pid = await _create_project(client, "环境严格删除项目")
    env = await _create_env(client, pid, "prod")
    script_id = await _create_script(client, db_session, pid, "s10")
    await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "引用环境场景",
            "scenario_type": "混合场景",
            "environment_id": env["id"],
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    resp = await client.delete(
        f"/api/v1/projects/{pid}/environments/{env['id']}", headers=_auth("alice")
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3043


async def test_environment_delete_force_unbinds_scenarios(client, db_session) -> None:
    """环境删除 force：先解绑引用场景（environment_id 置 null）再删环境。"""
    pid = await _create_project(client, "环境force删除项目")
    env = await _create_env(client, pid, "prod")
    script_id = await _create_script(client, db_session, pid, "s11")
    create = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "force解绑场景",
            "scenario_type": "混合场景",
            "environment_id": env["id"],
            "scripts": [{"script_id": script_id, "thread_groups": [{"thread_group_name": "tg1"}]}],
        },
        headers=_auth("alice"),
    )
    sid = create.json()["data"]["id"]

    resp = await client.delete(
        f"/api/v1/projects/{pid}/environments/{env['id']}?force=true",
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["deleted"] is True
    assert body["force"] is True
    assert body["removed_scenarios"] == 1

    # 场景仍存在但 environment_id 已解绑
    detail = await client.get(
        f"/api/v1/projects/{pid}/scenarios/{sid}", headers=_auth("alice")
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["environment_id"] is None


async def test_environment_delete_force_zero_when_unbound(client, db_session) -> None:
    """环境删除 force：无引用时 removed_scenarios=0。"""
    pid = await _create_project(client, "环境force零引用项目")
    env = await _create_env(client, pid, "dev")
    resp = await client.delete(
        f"/api/v1/projects/{pid}/environments/{env['id']}?force=true",
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["removed_scenarios"] == 0
