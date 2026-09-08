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
        for df in (script.data_files or []):
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
                    "url": await storage.presigned_put(f"runs/{run_no}/{aid}/result.jtl"),
                },
                "report": {
                    "key": f"runs/{run_no}/{aid}/report.zip",
                    "url": await storage.presigned_put(f"runs/{run_no}/{aid}/report.zip"),
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


async def dispatch(run_no: str, agent_ids: list[str], task_data: dict, upload_urls: dict) -> None:
    """向选中 Agent 下发任务（每个 Agent 嵌入自己的上传目标），按实际送达情况更新执行记录。"""
    sent = []
    for aid in agent_ids:
        payload = {**task_data, "upload": upload_urls.get(aid, {})}
        message = Envelope.now(MSG_TASK, payload).model_dump()
        if await agent_manager.send(aid, message):
            sent.append(aid)

    if sent:
        _pending_results[run_no] = {"agents": set(sent), "done": set(), "failed": set()}
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
        run = (await db.execute(select(ScenarioRun).where(ScenarioRun.run_no == run_no))).scalars().first()
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


async def on_agent_result(run_no: str, agent_id: str, summary: dict, artifacts: list) -> None:
    """汇聚各 Agent 结果：全部收齐后合并写 ES 汇总索引并置终态。"""
    pending = _pending_results.get(run_no)
    if pending is None:
        logger.warning(f"收到未知 run 的结果: run={run_no} agent={agent_id}")
        return

    pending["done"].add(agent_id)
    if summary.get("failed"):
        pending["failed"].add(agent_id)
    logger.info(f"run={run_no} 收到 {agent_id} 结果 {len(pending['done'])}/{len(pending['agents'])}")

    if pending["done"] >= pending["agents"]:
        status = RunStatus.FINISHED if not pending["failed"] else RunStatus.PARTIAL
        # TODO P1: 各 Agent 分片 summary 按 label 合并去重后再写入
        await es_client.write_summary(
            run_no, {"agents": sorted(pending["done"]), "summary": summary, "artifacts": artifacts}
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
