"""前端实时推送通道：按 run_no 订阅/广播（指标批次、执行状态事件）。

前端通过 /ws/runs/{run_no}?token=... 接入，替代轮询 ES 拉曲线：
Agent 上报的 metrics 批次落 ES 的同时，经本 hub 实时推给订阅者。
"""

import asyncio

from fastapi import WebSocket
from loguru import logger


class FrontendHub:
    def __init__(self) -> None:
        # run_no -> 订阅连接集合
        self._subs: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, run_no: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._subs.setdefault(run_no, set()).add(websocket)
        logger.info(
            f"前端订阅实时通道: run={run_no}（当前 {len(self._subs[run_no])} 个）"
        )

    async def unsubscribe(self, run_no: str, websocket: WebSocket) -> None:
        async with self._lock:
            group = self._subs.get(run_no)
            if not group:
                return
            group.discard(websocket)
            if not group:
                self._subs.pop(run_no, None)

    async def publish(self, run_no: str, message: dict) -> None:
        """向某 run 的全部订阅连接推送消息；发送失败的连接顺手清理。"""
        async with self._lock:
            targets = list(self._subs.get(run_no, ()))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                group = self._subs.get(run_no)
                if group:
                    for ws in dead:
                        group.discard(ws)
                    if not group:
                        self._subs.pop(run_no, None)


# 全局单例
frontend_hub = FrontendHub()
