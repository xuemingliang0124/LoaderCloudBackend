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

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import RunStatus, RunTrigger
from app.models.run import ScenarioRun
from app.models.run_agent_result import RunAgentResult
from app.models.scenario import Scenario
from app.models.script import Script
from app.services import agent_registry, es_client, storage
from app.services.exceptions import BusinessError
from app.ws.hub import frontend_hub
from app.ws.manager import agent_manager
from app.ws.protocol import FE_MSG_RUN_STATUS, MSG_STOP, MSG_TASK, Envelope

# 非终态：处于这些状态的 run 才接受 Agent 结果/停止
_ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING)

# 停止看门狗：run_no -> 超时强制收尾任务（Agent 回报丢失/离线时兜底）
_watchdogs: dict[str, asyncio.Task] = {}


async def create_run(
    scenario_id: int,
    trigger: RunTrigger,
    agent_ids: list[str] | None = None,
    created_by: str = "",
) -> dict:
    """创建执行记录并下发任务，返回 run_no 与实际选中的 Agent。"""
    async with SessionLocal() as db:
        scenario = await db.get(Scenario, scenario_id)
        if scenario is None:
            raise BusinessError("场景不存在", code=2001)
        if not agent_ids:
            agent_ids = await agent_registry.select_agents(
                scenario.agent_tags or [], scenario.agent_count
            )
        if not agent_ids:
            raise BusinessError("无可用压力机，请先上线 Agent", code=2002)

        run_no = f"r{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        db.add(
            ScenarioRun(
                run_no=run_no,
                scenario_id=scenario_id,
                status=RunStatus.PENDING,
                trigger=trigger,
                agent_ids=agent_ids,
                created_by=created_by,
            )
        )
        await db.commit()

        script = await db.get(Script, scenario.script_id)
        if script is None or not script.file_key:
            raise BusinessError("场景关联脚本不存在或缺少脚本文件", code=2005)

        jmeter_args = {**(scenario.param_overrides or {})}
        if scenario.duration:
            jmeter_args["duration"] = str(scenario.duration)

        # 文件下载清单：Master 预签 GET URL，Agent 直连 MinIO 下载
        # 先放 JMX 脚本，再追加 JMX 引用的数据文件（CSV 等），save_as 用上传时的原始文件名
        files = [
            {
                "key": script.file_key,
                "save_as": script.file_key.rsplit("/", 1)[-1],
                "url": await storage.presigned_get(script.file_key),
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

        # 插件由 PluginSyncer 在 Agent 启动/在线推送时对齐 plugin_dir，
        # 任务下发不再比对、不携带 plugins 字段
        nodes = await agent_registry.get_nodes(agent_ids)

        # 线程按压力机规格（CPU 核数）拆分：total_threads>0 时各机 -Jthreads 不同
        agent_thread_args: dict[str, dict] = {}
        if scenario.total_threads and len(agent_ids) > 1:
            if scenario.total_threads < len(agent_ids):
                logger.warning(
                    f"run={run_no} 总线程数 {scenario.total_threads} 少于 "
                    f"Agent 数 {len(agent_ids)}，部分 Agent 将分到 0 线程"
                )
            weights = {
                aid: max(1, (nodes[aid].cpu_cores or 0)) if aid in nodes else 1
                for aid in agent_ids
            }
            shares = split_threads(scenario.total_threads, weights)
            agent_thread_args = {aid: {"threads": str(n)} for aid, n in shares.items()}
            logger.info(f"run={run_no} 线程按规格拆分: {shares}")

        # 各 Agent 产物上传目标（预签 PUT URL 按 agent 独立签发，路径含 agent_id）
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
            for aid in agent_ids
        }
        task_data = {
            "run_id": run_no,
            "files": files,
            "jmeter_args": jmeter_args,
            "start_at": int(time.time()) + 5,
        }

    await dispatch(
        run_no,
        agent_ids,
        task_data,
        upload_urls,
        agent_thread_args,
    )
    return {"run_no": run_no, "agent_ids": agent_ids}


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
    """合并多 Agent 的 summary，按 label 聚合。

    合并口径：
    - samples/errors：直接累加（各 Agent 独立加压，总量有意义）
    - p95_rt：取 max（保守口径，代表整体瓶颈；跨进程精确 p95 需各 Agent 上报
      延迟分桶或原始延迟数组，留 P2）
    - max_tps：取 max（峰值不累加，因各 Agent 时间轴可能错峰；真实聚合峰值
      需各 Agent 上报 interval 级 tps 序列对齐求和，留 P2）
    - by_label：union 所有 label，按 samples 降序
    """
    totals = {"samples": 0, "errors": 0, "p95_rt": 0.0, "max_tps": 0.0}
    by_label: dict[str, dict] = {}
    for s in summaries:
        totals["samples"] += int(s.get("samples") or 0)
        totals["errors"] += int(s.get("errors") or 0)
        totals["p95_rt"] = max(totals["p95_rt"], float(s.get("p95_rt") or 0.0))
        totals["max_tps"] = max(totals["max_tps"], float(s.get("max_tps") or 0.0))
        for item in s.get("by_label") or []:
            label = item.get("label") or "_unknown"
            bucket = by_label.setdefault(
                label,
                {
                    "label": label,
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
    run_no: str, agent_id: str, summary: dict, artifacts: list
) -> None:
    """汇聚各 Agent 结果：落 run_agent_result 表；收齐后合并写 ES 汇总并置终态。"""
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
        agent_ids = list(run.agent_ids or [])
        stopping = run.status == RunStatus.STOPPING

        existing = (
            (
                await db.execute(
                    select(RunAgentResult).where(
                        RunAgentResult.run_no == run_no,
                        RunAgentResult.agent_id == agent_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            existing.summary = summary
            existing.artifacts = artifacts
            existing.failed = failed
        else:
            db.add(
                RunAgentResult(
                    run_no=run_no,
                    agent_id=agent_id,
                    summary=summary,
                    artifacts=artifacts,
                    failed=failed,
                )
            )
        await db.commit()

    logger.info(f"run={run_no} 收到 {agent_id} 结果（failed={failed}）")
    await _maybe_finalize(run_no, agent_ids, stopping)


async def _maybe_finalize(run_no: str, agent_ids: list[str], stopping: bool) -> None:
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
        if agent_ids and len(rows) < len(agent_ids):
            logger.info(f"run={run_no} 结果汇聚 {len(rows)}/{len(agent_ids)}，继续等待")
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
    """停止看门狗：超时未回报的 Agent 记失败占位，强制收尾 STOPPED。

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
        reported = {r.agent_id for r in rows}
        for aid in agent_ids:
            if aid in reported:
                continue
            logger.warning(f"run={run_no} agent={aid} 停止超时未回报，记为失败")
            db.add(
                RunAgentResult(
                    run_no=run_no,
                    agent_id=aid,
                    summary={
                        "failed": True,
                        "message": "停止超时未回报（Agent 可能离线）",
                    },
                    artifacts=[],
                    failed=True,
                )
            )
        await db.commit()
    await _maybe_finalize(run_no, agent_ids, stopping=True)


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
        pending = [(r.run_no, r.status, list(r.agent_ids or [])) for r in runs]

    for run_no, status, agent_ids in pending:
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
        if agent_ids and len(rows) >= len(agent_ids):
            logger.info(f"run={run_no} 重启前结果已收齐，直接收尾")
            await _maybe_finalize(
                run_no, agent_ids, stopping=status == RunStatus.STOPPING
            )
        elif status == RunStatus.STOPPING:
            logger.warning(f"run={run_no} 重启时停止中且结果未齐，布防看门狗兜底")
            _arm_stop_watchdog(run_no, get_settings().stop_wait_timeout)
        else:
            logger.info(
                f"run={run_no} 重启恢复：等待 Agent 重连后续报结果 "
                f"（{len(rows)}/{len(agent_ids)}）"
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
