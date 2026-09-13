"""场景管理测试：项目嵌套路由鉴权门禁/参数校验/旧路径下线（不落库）、
落库后的调度器与运行时长覆盖行为（aiosqlite）、执行期生效设置单元测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import app
from app.models.enums import ScenarioType
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.services.orchestrator import _effective_thread_group_settings

_TOKEN = create_access_token("tester", "viewer")

_CLIENT_KWARGS = {"transport": ASGITransport(app=app), "base_url": "http://test"}


def _auth(username: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {create_access_token(username, role)}"}


async def _create_project(client, name: str, username: str = "alice") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"name": name}, headers=_auth(username)
    )
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/projects/1/scenarios"),
        ("POST", "/api/v1/projects/1/scenarios"),
        ("GET", "/api/v1/projects/1/scenarios/1"),
        ("PUT", "/api/v1/projects/1/scenarios/1"),
        ("GET", "/api/v1/projects/1/scenarios/1/delete-precheck"),
        ("DELETE", "/api/v1/projects/1/scenarios/1"),
    ],
)
async def test_scoped_scenario_endpoints_require_token(method: str, path: str) -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/v1/scenarios"), ("POST", "/api/v1/scenarios")]
)
async def test_legacy_flat_scenario_paths_removed(method: str, path: str) -> None:
    # 旧扁平路径已下线：无路由匹配 → 404
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.request(method, path)
    assert resp.status_code == 404


async def test_create_scenario_missing_name_rejected() -> None:
    # name 必填：422 在进入 handler（项目存在性校验）前失败，不会访问数据库
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.post(
            "/api/v1/projects/1/scenarios",
            json={"scripts": []},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_scenarios_invalid_project_id_type_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/not-an-int/scenarios",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_list_scenarios_invalid_page_rejected() -> None:
    async with AsyncClient(**_CLIENT_KWARGS) as client:
        resp = await client.get(
            "/api/v1/projects/1/scenarios?page=0",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422


# ---- 落库行为：scheduler 统一 True，duration 用场景级运行时间覆盖 ----


async def test_create_scenario_forces_scheduler_and_scenario_duration(
    client, db_session
) -> None:
    """线程组不再接收 scheduler/duration：落库统一 True + 场景运行时间，旧字段静默忽略。"""
    pid = await _create_project(client, "场景创建项目")
    script = Script(project_id=pid, name="s1", file_key="scripts/x/v1/a.jmx")
    db_session.add(script)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "负载场景",
            "scenario_type": "单交易负载",
            "duration": 600,
            "scripts": [
                {
                    "script_id": script.id,
                    "thread_groups": [
                        {
                            "thread_group_name": "tg1",
                            "num_threads": 10,
                            "ramp_time": 5,
                            "tps": 100,
                            # 旧客户端仍传 loops/scheduler/duration 时被忽略
                            "loops": -1,
                            "scheduler": False,
                            "duration": 999,
                        }
                    ],
                }
            ],
        },
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    tg = resp.json()["data"]["scripts"][0]["thread_groups"][0]
    assert tg["scheduler"] is True
    assert tg["duration"] == 600
    assert tg["num_threads"] == 10
    assert tg["tps"] == 100
    assert tg["enabled"] is True  # 缺省启用
    # loops 已移除：响应中不再出现
    assert "loops" not in tg


async def test_update_scenario_reapplies_scenario_duration(client, db_session) -> None:
    """更新场景后线程组 duration 重新对齐新的场景级运行时间。"""
    pid = await _create_project(client, "场景更新项目")
    script = Script(project_id=pid, name="s2", file_key="scripts/x/v1/b.jmx")
    db_session.add(script)
    await db_session.commit()

    payload = {
        "scenario_type": "混合场景",
        "scripts": [
            {"script_id": script.id, "thread_groups": [{"thread_group_name": "tg1"}]}
        ],
    }
    resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={**payload, "name": "更新前场景", "duration": 300},
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    scenario_id = resp.json()["data"]["id"]

    resp = await client.put(
        f"/api/v1/projects/{pid}/scenarios/{scenario_id}",
        json={**payload, "name": "更新后场景", "duration": 1200},
        headers=_auth("alice"),
    )
    assert resp.status_code == 200
    tg = resp.json()["data"]["scripts"][0]["thread_groups"][0]
    assert tg["scheduler"] is True
    assert tg["duration"] == 1200


# ---- 场景详情 ----


async def test_get_scenario_detail_returns_full_graph(client, db_session) -> None:
    """详情回读：基础信息 + 脚本名/选机 + 线程组参数，结构与创建响应一致。"""
    pid = await _create_project(client, "详情项目")
    script = Script(project_id=pid, name="详情脚本", file_key="scripts/x/v1/c.jmx")
    db_session.add(script)
    await db_session.commit()

    create_resp = await client.post(
        f"/api/v1/projects/{pid}/scenarios",
        json={
            "name": "详情场景",
            "scenario_type": "混合场景",
            "duration": 900,
            "param_overrides": {"host": "api.demo.com"},
            "description": "详情用",
            "scripts": [
                {
                    "script_id": script.id,
                    "order_index": 0,
                    "agent_tags": ["机房A"],
                    "agent_count": 2,
                    "thread_groups": [
                        {
                            "thread_group_name": "tg1",
                            "num_threads": 20,
                            "ramp_time": 8,
                            "tps": 150,
                            "enabled": False,
                        }
                    ],
                }
            ],
        },
        headers=_auth("alice"),
    )
    assert create_resp.status_code == 200
    scenario_id = create_resp.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/projects/{pid}/scenarios/{scenario_id}", headers=_auth("alice")
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == scenario_id
    assert data["project_id"] == pid
    assert data["name"] == "详情场景"
    assert data["scenario_type"] == "混合场景"
    assert data["duration"] == 900
    assert data["param_overrides"] == {"host": "api.demo.com"}
    assert data["description"] == "详情用"
    assert len(data["scripts"]) == 1
    ss = data["scripts"][0]
    assert ss["script_id"] == script.id
    assert ss["script_name"] == "详情脚本"
    assert ss["agent_tags"] == ["机房A"]
    assert ss["agent_count"] == 2
    tg = ss["thread_groups"][0]
    assert tg["thread_group_name"] == "tg1"
    assert tg["num_threads"] == 20
    assert tg["ramp_time"] == 8
    assert tg["tps"] == 150
    assert tg["enabled"] is False
    assert tg["scheduler"] is True
    assert tg["duration"] == 900


async def test_get_scenario_detail_missing_rejected_3013(client, db_session) -> None:
    pid = await _create_project(client, "详情不存在项目")

    resp = await client.get(
        f"/api/v1/projects/{pid}/scenarios/999999", headers=_auth("alice")
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3013


async def test_get_scenario_detail_cross_project_rejected_3022(
    client, db_session
) -> None:
    pid1 = await _create_project(client, "详情项目A")
    pid2 = await _create_project(client, "详情项目B")
    script = Script(project_id=pid1, name="s3", file_key="scripts/x/v1/d.jmx")
    db_session.add(script)
    await db_session.commit()
    create_resp = await client.post(
        f"/api/v1/projects/{pid1}/scenarios",
        json={"name": "跨项目场景", "scenario_type": "混合场景"},
        headers=_auth("alice"),
    )
    assert create_resp.status_code == 200
    scenario_id = create_resp.json()["data"]["id"]

    # 场景属于项目 A，从项目 B 访问 → 3022
    resp = await client.get(
        f"/api/v1/projects/{pid2}/scenarios/{scenario_id}", headers=_auth("alice")
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 3022


# ---- 执行期生效设置：非基准覆盖调度器/时长，基准固定参数不受影响 ----


def _tg_row(
    name: str = "tg1",
    num_threads: int = 50,
    ramp_time: int = 10,
    tps: int = 0,
    scheduler: bool = False,
    duration: int = 999,
    enabled: bool = True,
) -> ScenarioScriptTG:
    return ScenarioScriptTG(
        scenario_script_id=1,
        thread_group_name=name,
        testclass="ThreadGroup",
        enabled=enabled,
        num_threads=num_threads,
        ramp_time=ramp_time,
        tps=tps,
        scheduler=scheduler,
        duration=duration,
    )


@pytest.mark.parametrize(
    "stype", [ScenarioType.SINGLE_LOAD, ScenarioType.MIXED, ScenarioType.STABILITY]
)
def test_effective_settings_non_baseline_forced(stype: ScenarioType) -> None:
    """非基准场景：调度器强制 True，duration 用场景级运行时间覆盖保存值。"""
    eff = _effective_thread_group_settings([_tg_row(tps=80)], stype, 600)
    assert eff[0].scheduler is True
    assert eff[0].duration == 600
    # 其余字段沿用保存值
    assert eff[0].num_threads == 50
    assert eff[0].ramp_time == 10
    assert eff[0].tps == 80


def test_effective_settings_baseline_fixed_unaffected() -> None:
    """单交易基准固定参数不受场景级运行时间影响：线程 5/关闭调度器，tps 沿用。"""
    eff = _effective_thread_group_settings(
        [_tg_row(num_threads=50, tps=80, scheduler=True, duration=999)],
        ScenarioType.SINGLE_BASELINE,
        600,
    )
    assert eff[0].num_threads == 5
    assert eff[0].scheduler is False
    assert eff[0].duration == 0
    assert eff[0].ramp_time == 10  # ramp_time 沿用保存值
    assert eff[0].tps == 80  # tps 沿用保存值


def test_effective_settings_carries_enabled_flag() -> None:
    """启用状态不随场景类型变换，原样透传（由组装器写入节点属性）。"""
    eff = _effective_thread_group_settings(
        [_tg_row(enabled=False)], ScenarioType.MIXED, 600
    )
    assert eff[0].enabled is False
    eff_baseline = _effective_thread_group_settings(
        [_tg_row(enabled=False)], ScenarioType.SINGLE_BASELINE, 600
    )
    assert eff_baseline[0].enabled is False
