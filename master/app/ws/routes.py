"""WebSocket 端点：

- /ws/agent：Agent 控制通道。注册信息通过握手查询参数携带
  （agent_id/tags/ip/hostname/plugins/cpu_cores/mem_total_gb）。
- /ws/runs/{run_no}：前端实时通道（指标批次/状态推送），token 走 query 参数
  （浏览器 WS 不便设置 Authorization 头），替代轮询 ES。
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_token
from app.services import agent_registry
from app.ws.hub import frontend_hub
from app.ws.manager import agent_manager
from app.ws.protocol import FE_MSG_SUBSCRIBED, Envelope

router = APIRouter()


@router.websocket("/ws/agent")
async def agent_endpoint(
    websocket: WebSocket,
    agent_id: str,
    ip: str = "",
    hostname: str = "",
    tags: str = "",
    jmeter_version: str = "",
    plugins: str = "",
    cpu_cores: int = 0,
    mem_total_gb: float = 0.0,
) -> None:
    await agent_manager.connect(agent_id, websocket)
    await agent_registry.upsert_agent(
        agent_id,
        ip=ip,
        hostname=hostname,
        tags=[t for t in tags.split(",") if t],
        jmeter_version=jmeter_version,
        plugins=[p for p in plugins.split(",") if p],
        cpu_cores=cpu_cores,
        mem_total_gb=mem_total_gb,
    )
    try:
        while True:
            raw = await websocket.receive_json()
            await agent_manager.handle_message(agent_id, raw)
    except (WebSocketDisconnect, Exception):  # noqa: BLE001
        await agent_manager.disconnect(agent_id, websocket)


@router.websocket("/ws/runs/{run_no}")
async def run_stream_endpoint(
    websocket: WebSocket, run_no: str, token: str = ""
) -> None:
    """前端订阅某执行的实时指标/状态。JWT 走 query 参数；无效直接拒握手。"""
    if not decode_token(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await frontend_hub.subscribe(run_no, websocket)
    await websocket.send_json(
        Envelope.now(FE_MSG_SUBSCRIBED, {"run_no": run_no}).model_dump()
    )
    try:
        while True:
            # 前端无需上行数据；保持接收以感知断开（可扩展为 ping/pong）
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await frontend_hub.unsubscribe(run_no, websocket)
