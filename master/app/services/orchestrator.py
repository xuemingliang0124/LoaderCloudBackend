"""执行编排器：创建执行 → 选机 → 下发任务 → 汇聚结果 → 收尾。

状态流转：PENDING --下发成功--> RUNNING --全部 Agent result--> FINISHED/PARTIAL
         （下发全失败直接 FAILED；手动/定时停止走 STOPPING，收齐 stopped 回报
         或看门狗超时后再置 STOPPED）

结果汇聚持久化在 run_agent_result 表（每 Agent 一行），Master 重启后
recover_active_runs 据此恢复汇聚现场，不再依赖内存态。
"""

import asyncio
import time
import uuid
from datetime import datetime

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import RunStatus, RunTrigger, ScenarioType
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.scenario import Scenario
from app.models.scenario_script import ScenarioScript
from app.models.scenario_script_tg import ScenarioScriptTG
from app.services import agent_registry, es_client, jmx_assembler, storage
from app.services.exceptions import BusinessError
from app.ws.hub import frontend_hub
from app.ws.manager import agent_manager
from app.ws.protocol import FE_MSG_RUN_STATUS, MSG_STOP, MSG_TASK, Envelope

# 非终态：处于这些状态的 run 才接受 Agent 结果/停止
_ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING)

# 停止看门狗：run_no -> 超时强制收尾任务（Agent 回报丢失/离线时兜底）
_watchdogs: dict[str, asyncio.Task] = {}

# 单交易基准场景固定参数（优先级高于场景保存的线程组设置）
_BASELINE_NUM_THREADS = 5
_BASELINE_LOOPS = 100


def _effective_thread_group_settings(
    tgs: list[ScenarioScriptTG], scenario_type: ScenarioType
) -> list[ScenarioScriptTG]:
    """计算实际生效的线程组设置。

    单交易基准场景：线程数固定 5、循环 100、关闭调度器（duration 无效）；
    其余场景类型直接使用数据库保存值。ramp_time 沿用保存值。
    """
    if scenario_type != ScenarioType.SINGLE_BASELINE:
        return list(tgs)
    return [
        ScenarioScriptTG(
            scenario_script_id=tg.scenario_script_id,
            thread_group_name=tg.thread_group_name,
            testclass=tg.testclass,
            num_threads=_BASELINE_NUM_THREADS,
            ramp_time=tg.ramp_time,
            loops=_BASELINE_LOOPS,
            scheduler=False,
            duration=0,
        )
        for tg in tgs
    ]


async def create_run(
    scenario_id: int,
    trigger: RunTrigger,
    agent_ids: list[str] | None = None,
    created_by: str = "",
) -> dict:
    """创建执行记录并逐脚本下发任务，返回 run_no 与实际选中的 Agent 并集。

    每个脚本按自身 agent_tags/agent_count 独立选机；外部传入 agent_ids 时
    所有脚本共用该集合。线程按脚本内各线程组在脚本的 Agent 集合内按 CPU 拆分。
    """
    async with SessionLocal() as db:
        scenario = await db.get(Scenario, scenario_id)
        if scenario is None:
            raise BusinessError("场景不存在", code=2001)

        # 加载脚本（含脚本元数据）与各线程组设置
        scenario = (
            await db.execute(
                select(Scenario)
                .options(
                    selectinload(Scenario.scripts).selectinload(ScenarioScript.script)
                )
                .options(
                    selectinload(Scenario.scripts).selectinload(
                        ScenarioScript.thread_groups
                    )
                )
                .where(Scenario.id == scenario_id)
            )
        ).scalar_one()
        if not scenario.scripts:
            raise BusinessError("场景未关联任何脚本", code=2005)

        # 按脚本选机：外部指定 agent_ids 时所有脚本共用，否则每脚本独立选
        script_agents: dict[int, list[str]] = {}
        all_agent_ids: list[str] = []
        if agent_ids:
            for ss in scenario.scripts:
                script_agents[ss.id] = agent_ids
            all_agent_ids = list(agent_ids)
        else:
            for ss in scenario.scripts:
                aids = await agent_registry.select_agents(
                    ss.agent_tags or [], ss.agent_count
                )
                if not aids:
                    name = (
                        ss.script.name if ss.script is not None else str(ss.script_id)
                    )
                    raise BusinessError(f"脚本 {name} 无可用压力机", code=2002)
                script_agents[ss.id] = aids
                for aid in aids:
                    if aid not in all_agent_ids:
                        all_agent_ids.append(aid)
        if not all_agent_ids:
            raise BusinessError("无可用压力机，请先上线 Agent", code=2002)

        # 预期结果数 = 所有 (脚本, Agent) 下发对
        expected_results = sum(len(aids) for aids in script_agents.values())

        run_no = f"r{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        db.add(
            ScenarioRun(
                run_no=run_no,
                scenario_id=scenario_id,
                status=RunStatus.PENDING,
                trigger=trigger,
                agent_ids=all_agent_ids,
                expected_results=expected_results,
                created_by=created_by,
            )
        )
        await db.commit()

        # 场景级 JVM 参数覆盖（对所有脚本生效）
        base_args: dict[str, str] = {
            str(k): str(v) for k, v in (scenario.param_overrides or {}).items()
        }

        # 各 Agent 产物上传目标（预签 PUT URL 按 agent 独立签发）
        upload_urls = {
            aid: {
                "jtl": {
                    "key": f"runs/{run_no}/{aid}/result.jtl",
                    "url": await storage.presigned_put(
                        f"runs/{run_no}/{aid}/result.jtl"
                    ),
                },
                "report": {
                    "key": f"runs/{run_no}/{aid}/report.zip",
                    "url": await storage.presigned_put(
                        f"runs/{run_no}/{aid}/report.zip"
                    ),
                },
            }
            for aid in all_agent_ids
        }

    # 逐脚本组装执行用 JMX 并按 Agent 下发（插件由 PluginSyncer 对齐，不随任务下发）
    scenario_type = scenario.scenario_type
    start_at = int(time.time()) + 5
    for ss in scenario.scripts:
        aids = script_agents[ss.id]
        script = ss.script
        if script is None or not script.file_key:
            raise BusinessError(
                f"场景关联脚本不存在或缺少脚本文件: script_id={ss.script_id}",
                code=2005,
            )

        # 原始脚本只读，组装后写入 runs/{run_no}/ 供 Agent 下载
        original_bytes = await storage.get_object_bytes(script.file_key)
        save_name = script.file_key.rsplit("/", 1)[-1]
        # 场景级 -J 参数（host 等），所有 Agent 共用；线程参数已写入 XML，不再走 -J
        scenario_args = dict(base_args)

        # 生效设置（单交易基准固定参数在此统一处理，确保多 Agent 拆分基于固定值）
        effective_tgs = _effective_thread_group_settings(
            list(ss.thread_groups), scenario_type
        )

        # 线程数按 Agent CPU 核数拆分；每台 Agent 组装一份带各自线程数的 JMX
        nodes = await agent_registry.get_nodes(aids)
        if len(aids) > 1:
            weights = {
                aid: max(1, (nodes[aid].cpu_cores or 0)) if aid in nodes else 1
                for aid in aids
            }
            # 每个 Agent 对应一份设置副本，仅 num_threads 被拆分
            per_agent_tgs: dict[str, list[ScenarioScriptTG]] = {
                aid: [
                    ScenarioScriptTG(
                        scenario_script_id=tg.scenario_script_id,
                        thread_group_name=tg.thread_group_name,
                        testclass=tg.testclass,
                        num_threads=0,  # 稍后按拆分结果填充
                        ramp_time=tg.ramp_time,
                        loops=tg.loops,
                        scheduler=tg.scheduler,
                        duration=tg.duration,
                    )
                    for tg in effective_tgs
                ]
                for aid in aids
            }
            for tg in effective_tgs:
                if tg.num_threads <= 0:
                    continue
                shares = split_threads(tg.num_threads, weights)
                for aid, n in shares.items():
                    for ag_tg in per_agent_tgs[aid]:
                        if (
                            ag_tg.thread_group_name == tg.thread_group_name
                            and ag_tg.testclass == tg.testclass
                        ):
                            ag_tg.num_threads = n
            logger.info(
                f"run={run_no} script={ss.script_id} 线程拆分: "
                f"{ {tg.thread_group_name: tg.num_threads for tg in effective_tgs} }"
            )
        else:
            per_agent_tgs = {aids[0]: effective_tgs}

        # 为每个 Agent 组装 + 上传 JMX，并单独下发（保证每台执行对应线程份额）
        for aid in aids:
            assembled = jmx_assembler.assemble_jmx(
                original_bytes, per_agent_tgs[aid], scenario_type
            )
            jmx_key = f"runs/{run_no}/{ss.id}_{aid}.jmx"
            await storage.upload_bytes(jmx_key, assembled)
            files = [
                {
                    "key": jmx_key,
                    "save_as": save_name,
                    "url": await storage.presigned_get(jmx_key),
                }
            ]
            for df in script.data_files or []:
                files.append(
                    {
                        "key": df["key"],
                        "save_as": df["filename"],
                        "url": await storage.presigned_get(df["key"]),
                    }
                )
            # 线程参数已写入 XML，jmeter_args 仅保留场景级 -J 覆盖
            task_data = {
                "run_id": run_no,
                "scenario_script_id": ss.id,
                "files": files,
                "jmeter_args": dict(scenario_args),
                "start_at": start_at,
            }
            await dispatch(run_no, [aid], task_data, upload_urls)

    return {"run_no": run_no, "agent_ids": all_agent_ids}


async def dispatch(
    run_no: str,
    agent_ids: list[str],
    task_data: dict,
    upload_urls: dict,
    agent_args: dict | None = None,
) -> None:
    """向选中 Agent 下发任务（每 Agent 嵌入自己的上传目标/线程参数）。

    插件不再随任务下发：由 PluginSyncer 在 Agent 启动/在线推送时对齐 plugin_dir。
    """
    agent_args = agent_args or {}
    sent = []
    for aid in agent_ids:
        payload = {
            **task_data,
            "jmeter_args": {
                **task_data.get("jmeter_args", {}),
                **agent_args.get(aid, {}),
            },
            "upload": upload_urls.get(aid, {}),
        }
        message = Envelope.now(MSG_TASK, payload).model_dump()
        if await agent_manager.send(aid, message):
            sent.append(aid)

    async with SessionLocal() as db:
        await db.execute(
            update(ScenarioRun)
            .where(ScenarioRun.run_no == run_no)
            .values(
                status=RunStatus.RUNNING if sent else RunStatus.FAILED,
                start_time=datetime.now() if sent else None,
                error_message="" if sent else "任务下发失败：无可用 Agent 连接",
            )
        )
        await db.commit()
    logger.info(f"run={run_no} 下发完成 {len(sent)}/{len(agent_ids)}")


async def stop_run(run_no: str) -> None:
    """停止执行：广播 stop 指令（幂等），置 STOPPING 等待 Agent 回报终态。

    Agent 杀进程树后仍会上传产物并回报 result（summary.failed=True）；
    全部回报收齐（或看门狗超时兜底）后才置 STOPPED。
    """
    async with SessionLocal() as db:
        run = (
            (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
            .scalars()
            .first()
        )
        if run is None:
            raise BusinessError("执行记录不存在", code=2003)
        if run.status in (
            RunStatus.FINISHED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.STOPPED,
        ):
            raise BusinessError("执行已结束，无法停止", code=2006)
        if run.status == RunStatus.STOPPING:
            logger.info(f"run={run_no} 停止指令已在等待回报中，幂等返回")
            return
        agent_ids = run.agent_ids or []

        message = Envelope.now(MSG_STOP, {"run_id": run_no}).model_dump()
        sent = [aid for aid in agent_ids if await agent_manager.send(aid, message)]
        if not sent:
            logger.warning(
                f"run={run_no} 无在线 Agent 接收停止指令，等待看门狗超时收尾"
            )
        await db.execute(
            update(ScenarioRun)
            .where(ScenarioRun.run_no == run_no)
            .values(status=RunStatus.STOPPING)
        )
        await db.commit()

    _arm_stop_watchdog(run_no, get_settings().stop_wait_timeout)
    await _publish_run_status(run_no, RunStatus.STOPPING)
    logger.info(f"run={run_no} 已下发停止指令到 {len(sent)} 台 Agent，等待回报 stopped")


def split_threads(total: int, weights: dict[str, int]) -> dict[str, int]:
    """按权重（CPU 核数）用最大余数法把总线程拆到各 Agent，合计等于 total。

    权重为 0 的按 1 兜底；total < Agent 数时可能出现 0 线程的 Agent
    （配置不合理，调用方负责告警）。
    """
    if total <= 0 or not weights:
        return {}
    agents = sorted(weights, key=lambda a: weights[a], reverse=True)
    wsum = sum(max(1, w) for w in weights.values())
    raw = {a: total * max(1, weights[a]) / wsum for a in agents}
    shares = {a: int(raw[a]) for a in agents}
    leftover = total - sum(shares.values())
    # 小数部分最大的优先补 1
    for a in sorted(agents, key=lambda a: raw[a] - shares[a], reverse=True):
        if leftover <= 0:
            break
        shares[a] += 1
        leftover -= 1
    # total >= Agent 数时保证每台至少 1 线程：从份额最多的机器匀（总额不变）
    if total >= len(agents):
        for a in agents:
            if shares[a] >= 1:
                continue
            donors = [x for x in agents if shares[x] > 1]
            if not donors:
                break
            donor = max(donors, key=lambda x: shares[x])
            shares[donor] -= 1
            shares[a] += 1
    return shares


def _merge_summaries(summaries: list[dict]) -> dict:
    """合并多 Agent 的 summary，按 (label, sample_type) 聚合。

    合并口径：
    - samples/errors：直接累加（各 Agent 独立加压，总量有意义）
    - p95_rt：取 max（保守口径，代表整体瓶颈；跨进程精确 p95 需各 Agent 上报
      延迟分桶或原始延迟数组，留 P2）
    - max_tps：取 max（峰值不累加，因各 Agent 时间轴可能错峰；真实聚合峰值
      需各 Agent 上报 interval 级 tps 序列对齐求和，留 P2）
    - by_label：union 所有 (label, sample_type)，按 samples 降序；
      事务与取样器同名时因 sample_type 不同不会互相污染，旧 Agent 上报
      缺 sample_type 时按 request 兜底
    """
    totals = {"samples": 0, "errors": 0, "p95_rt": 0.0, "max_tps": 0.0}
    by_label: dict[tuple[str, str], dict] = {}
    for s in summaries:
        totals["samples"] += int(s.get("samples") or 0)
        totals["errors"] += int(s.get("errors") or 0)
        totals["p95_rt"] = max(totals["p95_rt"], float(s.get("p95_rt") or 0.0))
        totals["max_tps"] = max(totals["max_tps"], float(s.get("max_tps") or 0.0))
        for item in s.get("by_label") or []:
            label = item.get("label") or "_unknown"
            stype = item.get("sample_type") or "request"
            bucket = by_label.setdefault(
                (label, stype),
                {
                    "label": label,
                    "sample_type": stype,
                    "samples": 0,
                    "errors": 0,
                    "p95_rt": 0.0,
                    "max_tps": 0.0,
                },
            )
            bucket["samples"] += int(item.get("samples") or 0)
            bucket["errors"] += int(item.get("errors") or 0)
            bucket["p95_rt"] = max(bucket["p95_rt"], float(item.get("p95_rt") or 0.0))
            bucket["max_tps"] = max(
                bucket["max_tps"], float(item.get("max_tps") or 0.0)
            )
    by_label_sorted = sorted(
        by_label.values(), key=lambda x: x["samples"], reverse=True
    )
    return {
        "samples": totals["samples"],
        "errors": totals["errors"],
        "p95_rt": totals["p95_rt"],
        "max_tps": totals["max_tps"],
        "by_label": by_label_sorted,
    }


async def on_agent_result(
    run_no: str,
    agent_id: str,
    summary: dict,
    artifacts: list,
    scenario_script_id: int | None = None,
) -> None:
    """汇聚各 Agent 结果：落 run_agent_result 表；收齐后合并写 ES 汇总并置终态。

    同一 Agent 可执行场景内多个脚本，以 (run_no, agent_id, scenario_script_id)
    区分；缺失 scenario_script_id 时按 (run_no, agent_id) 单条处理（兼容旧 Agent）。
    """
    failed = bool(summary.get("failed"))
    async with SessionLocal() as db:
        run = (
            (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
            .scalars()
            .first()
        )
        if run is None:
            logger.warning(f"收到未知 run 的结果: run={run_no} agent={agent_id}")
            return
        if run.status not in _ACTIVE_STATUSES:
            logger.warning(
                f"收到已结束 run 的迟到结果: run={run_no} "
                f"agent={agent_id} status={run.status.value}"
            )
            return
        stopping = run.status == RunStatus.STOPPING
        expected = run.expected_results or len(run.agent_ids or [])

        # 按 (run_no, agent_id, scenario_script_id) 查找已有记录
        stmt = select(RunAgentResult).where(
            RunAgentResult.run_no == run_no,
            RunAgentResult.agent_id == agent_id,
        )
        if scenario_script_id is not None:
            stmt = stmt.where(RunAgentResult.scenario_script_id == scenario_script_id)
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            existing.summary = summary
            existing.artifacts = artifacts
            existing.failed = failed
        else:
            db.add(
                RunAgentResult(
                    run_no=run_no,
                    agent_id=agent_id,
                    scenario_script_id=scenario_script_id,
                    summary=summary,
                    artifacts=artifacts,
                    failed=failed,
                )
            )
        await db.commit()

    logger.info(f"run={run_no} 收到 {agent_id} 结果（failed={failed}）")
    await _maybe_finalize(run_no, expected, stopping)


async def _maybe_finalize(run_no: str, expected: int, stopping: bool) -> None:
    """结果收齐后合并写 ES 汇总并置终态；条件更新保证并发回报只收尾一次。"""
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(RunAgentResult).where(RunAgentResult.run_no == run_no)
                )
            )
            .scalars()
            .all()
        )
        if expected and len(rows) < expected:
            logger.info(f"run={run_no} 结果汇聚 {len(rows)}/{expected}，继续等待")
            return

        final_status = (
            RunStatus.STOPPED
            if stopping
            else (
                RunStatus.PARTIAL if any(r.failed for r in rows) else RunStatus.FINISHED
            )
        )
        # 原子抢占终态：只有从非终态更新成功的协程继续写 ES/推送
        result = await db.execute(
            update(ScenarioRun)
            .where(
                ScenarioRun.run_no == run_no,
                ScenarioRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(status=final_status, end_time=datetime.now())
        )
        if not result.rowcount:
            return

        merged_summary = _merge_summaries([r.summary or {} for r in rows])
        all_artifacts: list = []
        for r in sorted(rows, key=lambda x: x.agent_id):
            all_artifacts.extend(r.artifacts or [])
        await es_client.write_summary(
            run_no,
            {
                "agents": sorted(r.agent_id for r in rows),
                "failed_agents": sorted(r.agent_id for r in rows if r.failed),
                "summary": merged_summary,
                "artifacts": all_artifacts,
                "stopped": stopping,
            },
        )
        await db.commit()

    _cancel_stop_watchdog(run_no)
    await _publish_run_status(run_no, final_status)
    logger.info(f"run={run_no} 收官: {final_status.value}")


def _arm_stop_watchdog(run_no: str, timeout: int) -> None:
    _cancel_stop_watchdog(run_no)
    _watchdogs[run_no] = asyncio.create_task(_stop_watchdog(run_no, timeout))


def _cancel_stop_watchdog(run_no: str) -> None:
    task = _watchdogs.pop(run_no, None)
    if task is not None and not task.done():
        task.cancel()


async def _stop_watchdog(run_no: str, timeout: int) -> None:
    """停止看门狗：超时未回报的 (Agent, 脚本) 记失败占位，强制收尾 STOPPED。

    覆盖 Agent 离线/重启丢消息等异常，避免 run 永远停在 STOPPING。
    """
    await asyncio.sleep(timeout)
    async with SessionLocal() as db:
        run = (
            (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
            .scalars()
            .first()
        )
        if run is None or run.status != RunStatus.STOPPING:
            return
        expected = run.expected_results or len(run.agent_ids or [])
        agent_ids = list(run.agent_ids or [])
        rows = list(
            (
                await db.execute(
                    select(RunAgentResult).where(RunAgentResult.run_no == run_no)
                )
            )
            .scalars()
            .all()
        )
        # 补齐缺失的结果位（无法精确还原 (Agent, 脚本) 分配，用 scenario_script_id=NULL
        # 占位；MySQL 唯一约束允许多个 NULL，不会冲突）
        missing = expected - len(rows)
        for i in range(missing):
            aid = agent_ids[i % len(agent_ids)] if agent_ids else "unknown"
            logger.warning(
                f"run={run_no} agent={aid} 停止超时未回报，记为失败（{i + 1}/{missing}）"
            )
            db.add(
                RunAgentResult(
                    run_no=run_no,
                    agent_id=aid,
                    scenario_script_id=None,
                    summary={
                        "failed": True,
                        "message": "停止超时未回报（Agent 可能离线）",
                    },
                    artifacts=[],
                    failed=True,
                )
            )
        await db.commit()
    await _maybe_finalize(run_no, expected, stopping=True)


async def recover_active_runs() -> None:
    """Master 重启恢复：从 run_agent_result 重建汇聚现场。

    - PENDING（未下发完成）：直接置 FAILED
    - RUNNING/STOPPING：结果已收齐的立即收尾；STOPPING 未收齐的布防
      看门狗（Agent 可能在宕机期间已停止且回报丢失）；RUNNING 未收齐的
      保持现状，Agent 重连后会继续回报结果。
    """
    async with SessionLocal() as db:
        runs = list(
            (
                await db.execute(
                    select(ScenarioRun).where(ScenarioRun.status.in_(_ACTIVE_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        pending = [
            (r.run_no, r.status, r.expected_results or len(r.agent_ids or []))
            for r in runs
        ]

    for run_no, status, expected in pending:
        if status == RunStatus.PENDING:
            logger.warning(f"run={run_no} 重启时仍为 PENDING（未完成下发），置 FAILED")
            await _mark_run_failed(run_no, "Master 重启，任务未完成下发")
            continue
        async with SessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(RunAgentResult).where(RunAgentResult.run_no == run_no)
                    )
                )
                .scalars()
                .all()
            )
        if expected and len(rows) >= expected:
            logger.info(f"run={run_no} 重启前结果已收齐，直接收尾")
            await _maybe_finalize(
                run_no, expected, stopping=status == RunStatus.STOPPING
            )
        elif status == RunStatus.STOPPING:
            logger.warning(f"run={run_no} 重启时停止中且结果未齐，布防看门狗兜底")
            _arm_stop_watchdog(run_no, get_settings().stop_wait_timeout)
        else:
            logger.info(
                f"run={run_no} 重启恢复：等待 Agent 重连后续报结果 "
                f"（{len(rows)}/{expected}）"
            )


async def _mark_run_failed(run_no: str, message: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(ScenarioRun)
            .where(
                ScenarioRun.run_no == run_no,
                ScenarioRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.FAILED,
                end_time=datetime.now(),
                error_message=message,
            )
        )
        await db.commit()
    await _publish_run_status(run_no, RunStatus.FAILED)


async def _publish_run_status(run_no: str, status: RunStatus) -> None:
    await frontend_hub.publish(
        run_no,
        Envelope.now(
            FE_MSG_RUN_STATUS,
            {"run_no": run_no, "status": status.value},
        ).model_dump(),
    )
