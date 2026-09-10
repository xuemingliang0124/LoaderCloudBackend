"""agent WebSocket 连接注册中心：连接管理、消息路由、下发通道。

分层说明：本模块属于服务层组件（ws 通道管理），消息处理依赖
agent_registry（注册中心）与 es_client（指标落库），结果汇聚通过
延迟 import orchestrator 避免循环依赖。Agent 上报的指标/状态在落库的同时
经 frontend_hub 实时推送给浏览器订阅者。
"""

import asyncio

from fastapi import WebSocket
from loguru import logger

from app.services import agent_registry, es_client
from app.ws.hub import frontend_hub
from app.ws.protocol import (
    FE_MSG_AGENT_STATUS,
    FE_MSG_METRICS,
    MSG_HEARTBEAT,
    MSG_METRICS,
    MSG_REGISTER,
    MSG_RESULT,
    MSG_STATUS,
    MSG_TASK_ACK,
    Envelope,
)


class AgentConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    def connected_ids(self) -> set[str]:
        return set(self._connections.keys())

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._connections

    async def connect(self, agent_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[agent_id] = websocket
        logger.info(f"Agent 上线: {agent_id}")

    async def disconnect(self, agent_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(agent_id) is websocket:
                self._connections.pop(agent_id, None)
        logger.info(f"Agent 断开: {agent_id}")
        await agent_registry.mark_offline(agent_id)

    async def send(self, agent_id: str, message: dict) -> bool:
        websocket = self._connections.get(agent_id)
        if websocket is None:
            return False
        try:
            await websocket.send_json(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"下发消息到 {agent_id} 失败: {exc}")
            return False

    async def broadcast(self, message: dict) -> list[str]:
        return [aid for aid in self.connected_ids() if await self.send(aid, message)]

    async def handle_message(self, agent_id: str, raw: dict) -> None:
        try:
            envelope = Envelope.model_validate(raw)
        except Exception:  # noqa: BLE001
            logger.warning(f"无法解析 {agent_id} 的消息: {raw!r}")
            return
        data = envelope.data
        if envelope.type == MSG_REGISTER:
            # 注册信息已在 WS 握手时落库（见 ws/routes.py），此处仅补刷心跳时间
            await agent_registry.touch_heartbeat(agent_id)
        elif envelope.type == MSG_HEARTBEAT:
            await agent_registry.touch_heartbeat(
                agent_id,
                cpu=float(data.get("cpu", 0.0)),
                mem=float(data.get("mem", 0.0)),
                current_run_no=data.get("current_run_id"),
                cpu_cores=int(data.get("cpu_cores", 0) or 0),
                mem_total_gb=float(data.get("mem_total_gb", 0.0) or 0.0),
            )
            # 心跳对账：Agent 上报 plugin_hashes，差异时推 sync/remove
            plugin_hashes = data.get("plugin_hashes") or []
            if plugin_hashes:
                from app.services.plugin_sync import on_heartbeat

                await on_heartbeat(agent_id, list(plugin_hashes))
        elif envelope.type == MSG_METRICS:
            await es_client.write_metrics({"agent_id": agent_id, **data})
            run_no = str(data.get("run_no", ""))
            if run_no:
                # 实时推给前端订阅者（替代轮询 ES）
                await frontend_hub.publish(
                    run_no,
                    Envelope.now(
                        FE_MSG_METRICS, {"agent_id": agent_id, **data}
                    ).model_dump(),
                )
        elif envelope.type == MSG_STATUS:
            logger.info(
                f"Agent[{agent_id}] run={data.get('run_id')} "
                f"phase={data.get('phase')} {data.get('message', '')}"
            )
            run_no = str(data.get("run_id", ""))
            if run_no:
                await frontend_hub.publish(
                    run_no,
                    Envelope.now(
                        FE_MSG_AGENT_STATUS, {"agent_id": agent_id, **data}
                    ).model_dump(),
                )
        elif envelope.type == MSG_TASK_ACK:
            logger.info(
                f"Agent[{agent_id}] 任务确认: {data.get('run_id')} "
                f"accepted={data.get('accepted')}"
            )
        elif envelope.type == "plugin_ack":
            # Agent 上报当前 plugin_dir 实际清单，刷新 agent_plugin 表
            from app.services.plugin_sync import on_plugin_ack

            await on_plugin_ack(agent_id, data.get("plugins") or [])
        elif envelope.type == MSG_RESULT:
            from app.services.orchestrator import (
                on_agent_result,
            )  # 延迟 import 防循环依赖

            await on_agent_result(
                run_no=str(data.get("run_id", "")),
                agent_id=agent_id,
                summary=data.get("summary", {}),
                artifacts=data.get("artifacts", []),
                scenario_script_id=data.get("scenario_script_id"),
            )
        else:
            logger.debug(f"忽略 {agent_id} 的未知消息类型: {envelope.type}")


# 全局单例
agent_manager = AgentConnectionManager()
