"""Agent WebSocket 端点：/ws/agent

注册信息通过握手查询参数携带（Agent 首帧 register 作为心跳补刷）。
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services import agent_registry
from app.ws.manager import agent_manager

router = APIRouter()


@router.websocket("/ws/agent")
async def agent_endpoint(
    websocket: WebSocket,
    agent_id: str,
    ip: str = "",
    hostname: str = "",
    tags: str = "",
    jmeter_version: str = "",
) -> None:
    await agent_manager.connect(agent_id, websocket)
    await agent_registry.upsert_agent(
        agent_id,
        ip=ip,
        hostname=hostname,
        tags=[t for t in tags.split(",") if t],
        jmeter_version=jmeter_version,
    )
    try:
        while True:
            raw = await websocket.receive_json()
            await agent_manager.handle_message(agent_id, raw)
    except (WebSocketDisconnect, Exception):  # noqa: BLE001
        await agent_manager.disconnect(agent_id, websocket)
