"""LLM 工具真实后端接线测试（FR-06 Stage 4）。

经内存 SQLite（backends.SessionLocal 重绑定到 db_env maker）验证工具全链路：
@tool → _dispatch → DB/ES 后端。ES/MinIO 调用以 monkeypatch 替身注入，
不依赖真实基础设施；错误分支返回结构化 error dict（带业务 code）。
"""

import pytest_asyncio

from app.models.enums import RunStatus
from app.models.environment import Environment
from app.models.project import Project
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.models.transaction import Transaction
from app.services import es_client, storage
from app.services.llm.backends import register_tool_backends
from app.services.llm.tools import (
    create_scenario as create_scenario_tool,
)
from app.services.llm.tools import (
    get_realtime_summary,
    get_run_summary,
    get_scenario,
    query_environments,
    query_metrics,
    query_projects,
    query_transactions,
    reset_backends,
)

# 单线程组 JMX：num_threads=50、ramp=30，无吞吐量定时器
_JMX_STR = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="用户登录" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">10</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">50</stringProp>
        <stringProp name="ThreadGroup.ramp_time">30</stringProp>
        <boolProp name="ThreadGroup.scheduler">false</boolProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""
_JMX_BYTES = _JMX_STR.encode("utf-8")


@pytest_asyncio.fixture
async def wired(db_env, monkeypatch):
    """把后端 SessionLocal 重绑定到内存库 maker，注册真实后端；用例后清空。"""
    import app.services.llm.backends as backends

    monkeypatch.setattr(backends, "SessionLocal", db_env)
    register_tool_backends()
    yield db_env
    reset_backends()


async def _seed_project(db_session, name: str = "电商平台") -> Project:
    project = Project(name=name, description="压测项目", created_by="admin")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------- query_projects ----------


async def test_query_projects_lists_all(wired, db_session) -> None:
    await _seed_project(db_session, "电商平台")
    await _seed_project(db_session, "支付网关")

    result = await query_projects.ainvoke({})
    assert result["total"] == 2
    assert {item["name"] for item in result["items"]} == {"电商平台", "支付网关"}
    first = result["items"][0]
    assert {"id", "name", "description", "created_by"} <= set(first)


async def test_query_projects_fuzzy_by_name(wired, db_session) -> None:
    await _seed_project(db_session, "电商平台")
    await _seed_project(db_session, "支付网关")

    result = await query_projects.ainvoke({"name": "电商"})
    assert result["total"] == 1
    assert result["items"][0]["name"] == "电商平台"


# ---------- query_environments ----------


async def test_query_environments_filter(wired, db_session) -> None:
    project = await _seed_project(db_session)
    db_session.add(
        Environment(
            project_id=project.id, name="生产环境", env_code="prod", base_url=""
        )
    )
    await db_session.commit()

    result = await query_environments.ainvoke({"project_id": project.id})
    assert result["total"] == 1
    assert result["items"][0]["env_code"] == "prod"

    filtered = await query_environments.ainvoke(
        {"project_id": project.id, "name": "生产"}
    )
    assert filtered["total"] == 1
    assert (
        await query_environments.ainvoke({"project_id": project.id, "name": "不存在"})
    )["total"] == 0


# ---------- query_transactions ----------


async def test_query_transactions_code_exact(wired, db_session) -> None:
    project = await _seed_project(db_session)
    db_session.add(
        Transaction(
            project_id=project.id,
            name="登录交易",
            txn_code="login",
            default_script_id=None,
        )
    )
    await db_session.commit()

    result = await query_transactions.ainvoke(
        {"project_id": project.id, "code": "login"}
    )
    assert result["total"] == 1
    assert result["items"][0]["name"] == "登录交易"
    miss = await query_transactions.ainvoke({"project_id": project.id, "code": "log"})
    assert miss["total"] == 0


# ---------- get_scenario ----------


async def _seed_scenario(db_session, project: Project, name: str = "登录场景") -> tuple:
    script = Script(
        project_id=project.id,
        name="登录脚本",
        version="v1",
        file_key="scripts/1/v1/login.jmx",
    )
    db_session.add(script)
    await db_session.flush()
    scenario = Scenario(project_id=project.id, name=name, duration=600)
    db_session.add(scenario)
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
    db_session.add(
        ScenarioScriptTG(
            scenario_script_id=ss.id,
            thread_group_name="用户登录",
            testclass="ThreadGroup",
            enabled=True,
            num_threads=50,
            ramp_time=30,
            tps=100,
            scheduler=True,
            duration=600,
        )
    )
    await db_session.commit()
    return scenario, script, ss


async def test_get_scenario_detail(wired, db_session) -> None:
    project = await _seed_project(db_session)
    scenario, _, _ = await _seed_scenario(db_session, project)

    result = await get_scenario.ainvoke({"scenario_id": scenario.id})
    assert result["id"] == scenario.id
    assert result["project_id"] == project.id
    assert len(result["scripts"]) == 1
    script_out = result["scripts"][0]
    assert script_out["script_name"] == "登录脚本"
    assert len(script_out["thread_groups"]) == 1
    assert script_out["thread_groups"][0]["tps"] == 100


async def test_get_scenario_missing_returns_error(wired) -> None:
    result = await get_scenario.ainvoke({"scenario_id": 999})
    assert result["error"]
    assert result["code"] == 3013


# ---------- create_scenario ----------


async def test_create_scenario_success(wired, db_session, monkeypatch) -> None:
    async def _fake_bytes(_key: str) -> bytes:
        return _JMX_BYTES

    monkeypatch.setattr(storage, "get_object_bytes", _fake_bytes)

    project = await _seed_project(db_session)
    env = Environment(
        project_id=project.id, name="生产环境", env_code="prod", base_url=""
    )
    db_session.add(env)
    script = Script(
        project_id=project.id,
        name="登录脚本",
        version="v1",
        file_key="scripts/1/v1/login.jmx",
    )
    db_session.add(script)
    await db_session.flush()
    txn = Transaction(
        project_id=project.id,
        name="登录交易",
        txn_code="login",
        default_script_id=script.id,
    )
    db_session.add(txn)
    await db_session.commit()
    await db_session.refresh(env)
    await db_session.refresh(txn)

    result = await create_scenario_tool.ainvoke(
        {
            "project_id": project.id,
            "name": "登录负载场景",
            "env_id": env.id,
            "txn_id": txn.id,
            "tps": 120.0,
            "duration_seconds": 600,
        }
    )
    assert "error" not in result
    assert result["name"] == "登录负载场景"
    assert result["scenario_type"] == "单交易负载"
    assert result["duration"] == 600
    assert result["environment_id"] == env.id
    tg = result["scripts"][0]["thread_groups"][0]
    assert tg["tps"] == 120
    assert tg["num_threads"] == 50  # 取脚本扫描默认值
    assert tg["ramp_time"] == 30

    # 落库验证
    from sqlalchemy import func, select

    assert (await db_session.scalar(select(func.count()).select_from(Scenario))) == 1
    assert (
        await db_session.scalar(select(func.count()).select_from(ScenarioScript))
    ) == 1
    assert (
        await db_session.scalar(select(func.count()).select_from(ScenarioScriptTG))
    ) == 1


async def test_create_scenario_project_missing(wired) -> None:
    result = await create_scenario_tool.ainvoke(
        {
            "project_id": 999,
            "name": "x",
            "env_id": 1,
            "txn_id": 1,
        }
    )
    assert result["code"] == 3021


async def test_create_scenario_env_cross_project(wired, db_session) -> None:
    project = await _seed_project(db_session)
    other = await _seed_project(db_session, "其他项目")
    db_session.add(
        Environment(project_id=other.id, name="他项目环境", env_code="x", base_url="")
    )
    await db_session.commit()

    from sqlalchemy import select

    env = (
        await db_session.execute(
            select(Environment).where(Environment.project_id == other.id)
        )
    ).scalar_one()
    result = await create_scenario_tool.ainvoke(
        {
            "project_id": project.id,
            "name": "x",
            "env_id": env.id,
            "txn_id": 1,
        }
    )
    assert result["code"] == 3042


async def test_create_scenario_txn_without_script(
    wired, db_session, monkeypatch
) -> None:
    async def _fake_bytes(_key: str) -> bytes:
        return _JMX_BYTES

    monkeypatch.setattr(storage, "get_object_bytes", _fake_bytes)

    project = await _seed_project(db_session)
    db_session.add(
        Environment(
            project_id=project.id, name="生产环境", env_code="prod", base_url=""
        )
    )
    db_session.add(
        Transaction(
            project_id=project.id,
            name="登录交易",
            txn_code="login",
            default_script_id=None,
        )
    )
    await db_session.commit()
    from sqlalchemy import select

    env = (
        await db_session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )
    ).scalar_one()
    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.project_id == project.id)
        )
    ).scalar_one()

    result = await create_scenario_tool.ainvoke(
        {
            "project_id": project.id,
            "name": "新场景",
            "env_id": env.id,
            "txn_id": txn.id,
        }
    )
    assert result["code"] == 3054


async def test_create_scenario_duplicate_name(wired, db_session, monkeypatch) -> None:
    async def _fake_bytes(_key: str) -> bytes:
        return _JMX_BYTES

    monkeypatch.setattr(storage, "get_object_bytes", _fake_bytes)

    project = await _seed_project(db_session)
    await _seed_scenario(db_session, project, name="重名场景")
    db_session.add(
        Environment(
            project_id=project.id, name="生产环境", env_code="prod", base_url=""
        )
    )
    script = Script(
        project_id=project.id,
        name="登录脚本",
        version="v1",
        file_key="scripts/1/v1/login.jmx",
    )
    db_session.add(script)
    await db_session.flush()
    db_session.add(
        Transaction(
            project_id=project.id,
            name="登录交易",
            txn_code="login",
            default_script_id=script.id,
        )
    )
    await db_session.commit()
    from sqlalchemy import select

    env = (
        await db_session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )
    ).scalar_one()
    txn = (
        await db_session.execute(
            select(Transaction).where(Transaction.project_id == project.id)
        )
    ).scalar_one()

    result = await create_scenario_tool.ainvoke(
        {
            "project_id": project.id,
            "name": "重名场景",
            "env_id": env.id,
            "txn_id": txn.id,
        }
    )
    assert result["code"] == 3010


# ---------- get_run_summary ----------


async def test_get_run_summary_terminal(wired, monkeypatch) -> None:
    doc = {"summary": {"samples": 100}, "agents": 2}

    async def _fake(_run_no: str):
        return doc

    monkeypatch.setattr(es_client, "query_summary", _fake)
    result = await get_run_summary.ainvoke({"run_no": "r-1"})
    assert result == doc


async def test_get_run_summary_active_2004(wired, db_session, monkeypatch) -> None:
    async def _none(_run_no: str):
        return None

    monkeypatch.setattr(es_client, "query_summary", _none)

    project = await _seed_project(db_session)
    scenario = Scenario(project_id=project.id, name="s1", duration=60)
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(
        ScenarioRun(
            run_no="r-2",
            scenario_id=scenario.id,
            status=RunStatus.RUNNING,
        )
    )
    await db_session.commit()

    result = await get_run_summary.ainvoke({"run_no": "r-2"})
    assert result["code"] == 2004
    assert "尚未结束" in result["error"]


async def test_get_run_summary_missing_2004(wired, monkeypatch) -> None:
    async def _none(_run_no: str):
        return None

    monkeypatch.setattr(es_client, "query_summary", _none)
    result = await get_run_summary.ainvoke({"run_no": "r-x"})
    assert result["code"] == 2004
    assert "不存在" in result["error"]


# ---------- get_realtime_summary ----------


async def test_get_realtime_summary(wired, db_session, monkeypatch) -> None:
    project = await _seed_project(db_session)
    scenario = Scenario(project_id=project.id, name="s1", duration=60)
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(
        ScenarioRun(
            run_no="r-3",
            scenario_id=scenario.id,
            status=RunStatus.RUNNING,
        )
    )
    await db_session.commit()

    async def _fake(_run_no: str) -> dict:
        return {"samples": 10, "avg_tps": 5.0}

    monkeypatch.setattr(es_client, "query_realtime_summary", _fake)
    result = await get_realtime_summary.ainvoke({"run_no": "r-3"})
    assert result == {"samples": 10, "avg_tps": 5.0}


async def test_get_realtime_summary_missing_run(wired) -> None:
    result = await get_realtime_summary.ainvoke({"run_no": "r-x"})
    assert result["code"] == 2003


# ---------- query_metrics ----------

_REALTIME = {
    "samples": 100,
    "errors": 2,
    "avg_tps": 50.2,
    "avg_rt": 35.5,
    "min_rt": 5.0,
    "max_rt": 80.0,
    "p95_rt": 70.1,
}


async def test_query_metrics_realtime(wired, db_session, monkeypatch) -> None:
    project = await _seed_project(db_session)
    scenario = Scenario(project_id=project.id, name="s1", duration=60)
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(
        ScenarioRun(
            run_no="r-4",
            scenario_id=scenario.id,
            status=RunStatus.RUNNING,
        )
    )
    await db_session.commit()

    async def _none(_run_no: str):
        return None

    async def _rt(_run_no: str) -> dict:
        return dict(_REALTIME)

    monkeypatch.setattr(es_client, "query_summary", _none)
    monkeypatch.setattr(es_client, "query_realtime_summary", _rt)

    avg = await query_metrics.ainvoke({"run_no": "r-4", "agg": "avg"})
    assert avg["source"] == "realtime"
    assert avg["samples"] == 100
    assert avg["response_time_ms"] == 35.5
    assert avg["tps"] == 50.2
    assert avg["error_rate"] == 2.0

    p95 = await query_metrics.ainvoke({"run_no": "r-4", "agg": "p95"})
    assert p95["response_time_ms"] == 70.1


async def test_query_metrics_prefers_terminal(wired, monkeypatch) -> None:
    terminal = {
        "samples": 100,
        "errors": 1,
        "avg_tps": 48.0,
        "avg_rt": 30.0,
        "min_rt": 4.0,
        "max_rt": 70.0,
        "p95_rt": 60.0,
    }

    async def _doc(_run_no: str):
        return {"summary": terminal}

    async def _rt(_run_no: str) -> dict:
        raise AssertionError("终态存在时不应查实时聚合")

    monkeypatch.setattr(es_client, "query_summary", _doc)
    monkeypatch.setattr(es_client, "query_realtime_summary", _rt)

    result = await query_metrics.ainvoke({"run_no": "r-5"})
    assert result["source"] == "summary"
    assert result["response_time_ms"] == 30.0


async def test_query_metrics_invalid_agg(wired) -> None:
    result = await query_metrics.ainvoke({"run_no": "r", "agg": "sum"})
    assert "error" in result


async def test_query_metrics_no_data_missing_run(wired, monkeypatch) -> None:
    async def _none(_run_no: str):
        return None

    async def _zero(_run_no: str) -> dict:
        return {"samples": 0}

    monkeypatch.setattr(es_client, "query_summary", _none)
    monkeypatch.setattr(es_client, "query_realtime_summary", _zero)

    result = await query_metrics.ainvoke({"run_no": "r-x"})
    assert result["code"] == 2003
