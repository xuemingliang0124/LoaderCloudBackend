"""执行编排器：创建执行 → 选机 → 下发任务 → 汇聚结果 → 收尾。

状态流转：PENDING --下发成功--> RUNNING --全部 Agent result--> FINISHED/PARTIAL
         （下发全失败直接 FAILED；手动/定时停止走 stop_run 置 STOPPED）
"""

import time
import uuid
from datetime import datetime

from loguru import logger
from sqlalchemy import select, update

from app.db.session import SessionLocal
from app.models.enums import RunStatus, RunTrigger
from app.models.run import ScenarioRun
from app.models.scenario import Scenario
from app.models.script import Script
from app.services import agent_registry, es_client, storage
from app.services.exceptions import BusinessError
from app.ws.manager import agent_manager
from app.ws.protocol import Envelope, MSG_STOP, MSG_TASK

# 结果汇聚内存态（骨架实现；P2 迁移到持久化以便 Master 重启恢复）
_pending_results: dict[str, dict] = {}


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

    await dispatch(run_no, agent_ids, task_data, upload_urls)
    return {"run_no": run_no, "agent_ids": agent_ids}


async def dispatch(
    run_no: str, agent_ids: list[str], task_data: dict, upload_urls: dict
) -> None:
    """向选中 Agent 下发任务（每个 Agent 嵌入自己的上传目标），按实际送达情况更新执行记录。"""
    sent = []
    for aid in agent_ids:
        payload = {**task_data, "upload": upload_urls.get(aid, {})}
        message = Envelope.now(MSG_TASK, payload).model_dump()
        if await agent_manager.send(aid, message):
            sent.append(aid)

    if sent:
        # results: agent_id -> {"summary": dict, "artifacts": list}
        # 收齐所有 Agent 后再合并写 pt-summary；骨架原版只存 done set 会丢失
        # 前面 Agent 的 summary/artifacts（最终写入的是最后一个 Agent 的数据）
        _pending_results[run_no] = {
            "agents": set(sent),
            "results": {},
            "failed": set(),
        }
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
    """停止执行：广播 stop 指令（幂等），Agent 杀进程树后回报。"""
    async with SessionLocal() as db:
        run = (
            (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no)))
            .scalars()
            .first()
        )
        if run is None:
            raise BusinessError("执行记录不存在", code=2003)
        agent_ids = run.agent_ids or []

    message = Envelope.now(MSG_STOP, {"run_id": run_no}).model_dump()
    sent = [aid for aid in agent_ids if await agent_manager.send(aid, message)]
    if not sent:
        raise BusinessError("停止指令下发失败：Agent 均不在线", code=2004)

    async with SessionLocal() as db:
        # TODO P2: 以 Agent 回报 stopped 为准再置终态，此处骨架先乐观置 STOPPED
        await db.execute(
            update(ScenarioRun)
            .where(ScenarioRun.run_no == run_no)
            .values(status=RunStatus.STOPPED, end_time=datetime.now())
        )
        await db.commit()
    _pending_results.pop(run_no, None)
    logger.info(f"run={run_no} 已下发停止指令到 {len(sent)} 台 Agent")


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
    """汇聚各 Agent 结果：全部收齐后按 label 合并写 ES 汇总索引并置终态。"""
    pending = _pending_results.get(run_no)
    if pending is None:
        logger.warning(f"收到未知 run 的结果: run={run_no} agent={agent_id}")
        return

    pending["results"][agent_id] = {"summary": summary, "artifacts": artifacts}
    if summary.get("failed"):
        pending["failed"].add(agent_id)
    done = set(pending["results"].keys())
    logger.info(
        f"run={run_no} 收到 {agent_id} 结果 {len(done)}/{len(pending['agents'])}"
    )

    if done >= pending["agents"]:
        status = RunStatus.FINISHED if not pending["failed"] else RunStatus.PARTIAL
        merged_summary = _merge_summaries(
            [r["summary"] for r in pending["results"].values()]
        )
        all_artifacts: list = []
        for aid in sorted(done):
            all_artifacts.extend(pending["results"][aid]["artifacts"])
        await es_client.write_summary(
            run_no,
            {
                "agents": sorted(done),
                "failed_agents": sorted(pending["failed"]),
                "summary": merged_summary,
                "artifacts": all_artifacts,
            },
        )
        async with SessionLocal() as db:
            await db.execute(
                update(ScenarioRun)
                .where(ScenarioRun.run_no == run_no)
                .values(status=status, end_time=datetime.now())
            )
            await db.commit()
        _pending_results.pop(run_no, None)
        logger.info(f"run={run_no} 收官: {status.value}")
