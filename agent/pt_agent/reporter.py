"""上报通道：WS 常驻客户端（断线指数退避重连）+ 心跳 + 各类发送封装。"""

import asyncio
import json
from urllib.parse import urlencode

import websockets
from loguru import logger

from pt_agent.collector import snapshot
from pt_agent.config import AgentSettings
from pt_agent.protocol import (
    MSG_HEARTBEAT,
    MSG_PING,
    MSG_PONG,
    MSG_REGISTER,
    MSG_STATUS,
    MSG_STOP,
    MSG_TASK,
    MSG_TASK_ACK,
    Envelope,
)
from pt_agent.state import AgentState


class Reporter:
    def __init__(self, settings: AgentSettings, state: AgentState) -> None:
        self._settings = settings
        self._state = state
        self._executor = None  # 延迟绑定（main 装配），避免与 executor 循环依赖
        self._ws = None
        self._connected = asyncio.Event()

    def bind_executor(self, executor) -> None:
        self._executor = executor

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def send(self, envelope: Envelope) -> bool:
        if not self.connected or self._ws is None:
            logger.warning(f"未连接 Master，丢弃消息: {envelope.type}")
            return False
        try:
            await self._ws.send(envelope.model_dump_json())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"消息发送失败({envelope.type}): {exc}")
            self._connected.clear()
            return False

    async def send_task_ack(self, run_id: str, accepted: bool, message: str = "") -> bool:
        return await self.send(
            Envelope.now(MSG_TASK_ACK, {"run_id": run_id, "accepted": accepted, "message": message})
        )

    async def send_status(self, run_id: str, phase, message: str = "") -> bool:
        return await self.send(
            Envelope.now(MSG_STATUS, {"run_id": run_id, "phase": phase.value, "message": message})
        )

    async def run_forever(self) -> None:
        """主循环：连接 → 注册 → 收消息；断线指数退避重连。"""
        settings = self._settings
        query = urlencode(
            {"agent_id": settings.resolved_agent_id, "tags": ",".join(settings.tag_list)}
        )
        url = f"{settings.master_ws_url}?{query}"
        delay = 1.0
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    self._connected.set()
                    delay = 1.0
                    logger.info(f"已连接 Master: {settings.master_ws_url}")
                    await self.send(
                        Envelope.now(MSG_REGISTER, {"agent_id": settings.resolved_agent_id})
                    )
                    heartbeat = asyncio.create_task(self._heartbeat_loop())
                    try:
                        await self._receive_loop(ws)
                    finally:
                        heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"与 Master 断开: {exc}，{delay:.0f}s 后重连")
            self._connected.clear()
            self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, settings.reconnect_delay_max)

    async def _heartbeat_loop(self) -> None:
        """周期上报心跳：资源快照 + 状态。"""
        while True:
            await asyncio.sleep(self._settings.heartbeat_interval)
            if not self.connected:
                continue
            data = snapshot()
            data.update(self._state.to_payload())
            await self.send(Envelope.now(MSG_HEARTBEAT, data))

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                envelope = Envelope.model_validate(
                    json.loads(raw) if isinstance(raw, (bytes, str)) else raw
                )
            except Exception:  # noqa: BLE001
                logger.warning(f"无法解析 Master 消息: {raw!r}")
                continue
            if envelope.type == MSG_PING:
                await self.send(Envelope.now(MSG_PONG))
            elif envelope.type == MSG_TASK:
                assert self._executor is not None, "executor 未绑定"
                await self._executor.submit(envelope.data)
            elif envelope.type == MSG_STOP:
                assert self._executor is not None, "executor 未绑定"
                await self._executor.stop(str(envelope.data.get("run_id", "")))
            else:
                logger.debug(f"忽略 Master 消息: {envelope.type}")
