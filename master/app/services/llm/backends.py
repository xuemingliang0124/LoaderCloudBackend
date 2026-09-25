"""Function Calling 工具真实后端（FR-06 Stage 4 接线）。

8 个静态工具的 DB/ES 实现：
- DB 类（query_projects / query_environments / query_transactions /
  get_scenario / create_scenario）：每次调用经 SessionLocal 开短会话（与
  agent_registry、orchestrator 等服务层惯例一致），用完即释放
- ES 类（get_run_summary / get_realtime_summary / query_metrics）：经
  es_client 读 pt-summary 终态文档、对 pt-metrics-* 做实时聚合

权限口径：工具只在对话链路内被 agent 调用，项目/执行可见性已由对话入口
（api chat / ws chat）的 ensure_project_access、ensure_run_visible 门禁
把关，工具后端不重复鉴权；对不存在/未结束/参数非法等情况返回结构化
error dict（业务错误带 code），让 LLM 能感知并降级回答（NFR-02）。

register_tool_backends() 在 lifespan 启动期一次性注册；注册幂等
（同名重复注册以最后一次为准）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import SessionLocal
from app.models.enums import ScenarioType
from app.models.environment import Environment
from app.models.project import Project
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.models.script import Script
from app.models.transaction import Transaction
from app.schemas import (
    EnvironmentOut,
    ProjectOut,
    ScenarioOut,
    ScenarioScriptOut,
    ThreadGroupSettingOut,
    TransactionOut,
)
from app.schemas.common import like_pattern
from app.services import es_client, storage
from app.services.jmx_scanner import scan_jmx
from app.services.llm.tools import register_backend
from app.services.orchestrator import _ACTIVE_STATUSES

# 工具返回清单的条数上限：LLM 上下文有限，避免全量灌入
_MAX_ITEMS = 100


# ---------- 场景响应构造（与 api/v1/scenarios 同构，分层约束下独立维护） ----------


def _build_scenario_out(scenario: Scenario) -> dict:
    """Scenario ORM（需预加载 scripts/script/thread_groups）→ ScenarioOut dict。

    与 api/v1/scenarios._build_scenario_out 同口径；服务层不可反向 import api，
    字段调整时两处需同步。
    """
    scripts_out: list[dict] = []
    for ss in scenario.scripts:
        script_name = ss.script.name if ss.script is not None else ""
        tgs = [
            ThreadGroupSettingOut.model_validate(tg).model_dump(mode="json")
            for tg in ss.thread_groups
        ]
        scripts_out.append(
            ScenarioScriptOut(
                id=ss.id,
                script_id=ss.script_id,
                order_index=ss.order_index,
                agent_tags=ss.agent_tags,
                agent_count=ss.agent_count,
                script_name=script_name,
                thread_groups=tgs,
            ).model_dump(mode="json")
        )
    return ScenarioOut(
        id=scenario.id,
        project_id=scenario.project_id,
        name=scenario.name,
        scenario_type=scenario.scenario_type,
        duration=scenario.duration,
        environment_id=scenario.environment_id,
        param_overrides=scenario.param_overrides,
        description=scenario.description,
        scripts=scripts_out,
    ).model_dump(mode="json")


def _scenario_detail_stmt(scenario_id: int):
    """构造回读场景及脚本/线程组关联的 select 语句（与 api 层同口径）。"""
    return (
        select(Scenario)
        .options(selectinload(Scenario.scripts).selectinload(ScenarioScript.script))
        .options(
            selectinload(Scenario.scripts).selectinload(ScenarioScript.thread_groups)
        )
        .where(Scenario.id == scenario_id)
    )


# ---------- DB 类工具 ----------


async def query_projects(name: str = "") -> dict:
    """按名称模糊查询项目列表；name 为空返回全部（上限 100 条，按 id 倒序）。"""
    async with SessionLocal() as db:
        filters = []
        if name:
            filters.append(Project.name.like(like_pattern(name.strip()), escape="\\"))
        rows = (
            (
                await db.execute(
                    select(Project)
                    .where(*filters)
                    .order_by(Project.id.desc())
                    .limit(_MAX_ITEMS)
                )
            )
            .scalars()
            .all()
        )
        items = [ProjectOut.model_validate(r).model_dump(mode="json") for r in rows]
    return {"total": len(items), "items": items}


async def query_environments(project_id: int, name: str = "") -> dict:
    """查询项目环境清单，可按环境名称模糊过滤（上限 100 条，按 id 倒序）。"""
    async with SessionLocal() as db:
        filters = [Environment.project_id == project_id]
        if name:
            filters.append(
                Environment.name.like(like_pattern(name.strip()), escape="\\")
            )
        rows = (
            (
                await db.execute(
                    select(Environment)
                    .where(*filters)
                    .order_by(Environment.id.desc())
                    .limit(_MAX_ITEMS)
                )
            )
            .scalars()
            .all()
        )
        items = [EnvironmentOut.model_validate(r).model_dump(mode="json") for r in rows]
    return {"total": len(items), "items": items}


async def query_transactions(project_id: int, code: str = "") -> dict:
    """查询项目交易清单，可按交易编码精确过滤（与 REST 列表口径一致，上限 100 条）。"""
    async with SessionLocal() as db:
        filters = [Transaction.project_id == project_id]
        if code:
            filters.append(Transaction.txn_code == code.strip())
        rows = (
            (
                await db.execute(
                    select(Transaction)
                    .where(*filters)
                    .order_by(Transaction.id.desc())
                    .limit(_MAX_ITEMS)
                )
            )
            .scalars()
            .all()
        )
        items = [TransactionOut.model_validate(r).model_dump(mode="json") for r in rows]
    return {"total": len(items), "items": items}


async def get_scenario(scenario_id: int) -> dict:
    """获取场景详情（脚本/线程组/绑定环境等）；不存在返回 error（对齐 3013）。"""
    async with SessionLocal() as db:
        scenario = (
            await db.execute(_scenario_detail_stmt(scenario_id))
        ).scalar_one_or_none()
        if scenario is None:
            return {"error": f"场景不存在: {scenario_id}", "code": 3013}
        return _build_scenario_out(scenario)


async def create_scenario(
    project_id: int,
    name: str,
    env_id: int,
    txn_id: int,
    tps: float = 0.0,
    duration_seconds: int = 0,
) -> dict:
    """基于「项目+环境+交易」自动创建单脚本压测场景。

    口径：
    - 依次校验项目（3021）、环境（3041/3042）、交易（3051/3052）；交易必须
      绑定默认脚本且脚本属于同项目（3054），否则无法自动建场景
    - 场景名唯一（3010），类型固定「单交易负载」，场景时长取 duration_seconds
    - 扫描默认脚本 JMX 取线程组清单：目标 tps 覆盖到启用线程组，其余加压
      参数（线程数/Ramp-up/启用状态）取脚本扫描默认值
    """
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None:
            return {"error": f"项目不存在: {project_id}", "code": 3021}

        env = await db.get(Environment, env_id)
        if env is None:
            return {"error": f"环境不存在: {env_id}", "code": 3041}
        if env.project_id != project_id:
            return {"error": "环境不属于指定项目", "code": 3042}

        txn = await db.get(Transaction, txn_id)
        if txn is None:
            return {"error": f"交易不存在: {txn_id}", "code": 3051}
        if txn.project_id != project_id:
            return {"error": "交易不属于指定项目", "code": 3052}

        script_id = txn.default_script_id
        if script_id is None:
            return {
                "error": f"交易 {txn.txn_code} 未绑定默认脚本，无法自动创建场景",
                "code": 3054,
            }
        script = await db.get(Script, script_id)
        if script is None or script.project_id != project_id:
            return {
                "error": "交易默认脚本不存在或不属于指定项目",
                "code": 3054,
            }

        dup_id = (
            await db.execute(select(Scenario.id).where(Scenario.name == name))
        ).scalar_one_or_none()
        if dup_id is not None:
            return {"error": f"场景名称已存在: {name}", "code": 3010}

        # 扫描脚本拿线程组清单（MinIO/JMX 异常会外抛，由 _dispatch 统一包装）
        scan = scan_jmx(await storage.get_object_bytes(script.file_key))

        scenario = Scenario(
            project_id=project_id,
            name=name,
            scenario_type=ScenarioType.SINGLE_LOAD,
            duration=duration_seconds,
            environment_id=env_id,
            param_overrides={},
        )
        db.add(scenario)
        await db.flush()

        target_tps = max(int(round(tps)), 0)
        ss = ScenarioScript(
            scenario_id=scenario.id,
            script_id=script.id,
            order_index=0,
            agent_tags=[],
            agent_count=1,
        )
        db.add(ss)
        await db.flush()
        for tg in scan.thread_groups:
            db.add(
                ScenarioScriptTG(
                    scenario_script_id=ss.id,
                    thread_group_name=tg.name,
                    testclass=tg.testclass,
                    enabled=tg.enabled,
                    num_threads=tg.num_threads,
                    ramp_time=tg.ramp_time,
                    # 禁用组不执行，限速无意义，置 0
                    tps=target_tps if tg.enabled else 0,
                    scheduler=True,
                    duration=duration_seconds,
                )
            )
        await db.commit()
        scenario = (await db.execute(_scenario_detail_stmt(scenario.id))).scalar_one()
        return _build_scenario_out(scenario)


# ---------- ES 类工具 ----------


async def get_run_summary(run_no: str) -> dict:
    """获取执行终态汇总；执行中返回 error（未结束），其余缺失返回 error（对齐 2004）。"""
    summary = await es_client.query_summary(run_no)
    if summary is not None:
        return summary
    async with SessionLocal() as db:
        run = (
            (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
            .scalars()
            .first()
        )
    if run is not None and run.status in _ACTIVE_STATUSES:
        return {"error": "执行尚未结束，汇总未生成", "code": 2004}
    return {"error": "执行汇总不存在", "code": 2004}


async def get_realtime_summary(run_no: str) -> dict:
    """获取执行期实时汇总（执行中可查）；执行记录不存在返回 error（对齐 2003）。"""
    async with SessionLocal() as db:
        run_id = (
            await db.execute(select(ScenarioRun.id).where(ScenarioRun.run_no == run_no))
        ).scalar_one_or_none()
    if run_id is None:
        return {"error": f"执行记录不存在: {run_no}", "code": 2003}
    return await es_client.query_realtime_summary(run_no)


# agg → 聚合结构中的响应时间字段
_METRIC_AGG_FIELDS = {
    "avg": "avg_rt",
    "max": "max_rt",
    "min": "min_rt",
    "p95": "p95_rt",
}


async def query_metrics(run_no: str, agg: str = "avg") -> dict:
    """查询执行核心指标（响应时间/TPS/错误率/样本数），agg 取值 avg/max/min/p95。

    数据优先取终态 pt-summary（精确值）；终态缺失时兜底 pt-metrics 实时聚合。
    """
    rt_field = _METRIC_AGG_FIELDS.get(agg)
    if rt_field is None:
        return {"error": f"不支持的聚合方式: {agg}（可选 avg/max/min/p95）"}

    source = "summary"
    data: dict | None = None
    summary_doc = await es_client.query_summary(run_no)
    if summary_doc is not None:
        terminal = summary_doc.get("summary") or {}
        if int(terminal.get("samples") or 0) > 0:
            data = terminal
    if data is None:
        source = "realtime"
        data = await es_client.query_realtime_summary(run_no)

    samples = int(data.get("samples") or 0)
    if samples <= 0:
        async with SessionLocal() as db:
            run_id = (
                await db.execute(
                    select(ScenarioRun.id).where(ScenarioRun.run_no == run_no)
                )
            ).scalar_one_or_none()
        if run_id is None:
            return {"error": f"执行记录不存在: {run_no}", "code": 2003}
        return {"error": f"执行 {run_no} 暂无指标数据", "code": 2004}

    errors = int(data.get("errors") or 0)
    return {
        "run_no": run_no,
        "agg": agg,
        "source": source,
        "samples": samples,
        "response_time_ms": round(float(data.get(rt_field) or 0.0), 2),
        "tps": round(float(data.get("avg_tps") or 0.0), 2),
        "error_rate": round(errors / samples * 100, 4),
    }


def register_tool_backends() -> None:
    """注册全部 8 个静态工具的真实后端（lifespan 启动期调用，重复调用幂等）。"""
    register_backend("query_projects", query_projects)
    register_backend("query_environments", query_environments)
    register_backend("query_transactions", query_transactions)
    register_backend("get_scenario", get_scenario)
    register_backend("create_scenario", create_scenario)
    register_backend("get_run_summary", get_run_summary)
    register_backend("get_realtime_summary", get_realtime_summary)
    register_backend("query_metrics", query_metrics)
