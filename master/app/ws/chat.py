"""LLM 对话 WebSocket 通道（FR-10，SRS 6.2）：/ws/chat。

独立于 /ws/agent 与 /ws/runs：不复用 agent_manager / frontend_hub，
由本模块自有的 ChatConnectionManager 管理连接（实施方案风险表：避免
Agent 控制通道与对话通道耦合）。

协议：
- 握手：JWT 走 query 参数 token（浏览器 WS 不便设置 Authorization 头），
  无效 token → close 1008，不泄露具体原因
- 上行（每条一轮问答，支持单连接多轮）：
  {"project_id":1, "message":"...", "use_tools":true, "top_k":5,
   "run_no":"...（可选）"}
- 下行：orchestrator.astream_qa_events 的事件流
  token / tool_call / error / done（done.final = FR-09 五字段）
- 项目门禁/run_no 校验在每轮问答前执行；不通过 → error 事件 + 1008 关闭
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from app.api.deps import CurrentUser, ensure_project_access, ensure_run_visible
from app.core.config import ERR_METRIC_VALIDATION_FAILED
from app.core.security import decode_token_payload
from app.db.session import SessionLocal
from app.services.exceptions import BusinessError
from app.services.llm.orchestrator import astream_qa_events

router = APIRouter()


class ChatConnectionManager:
    """对话 WS 连接管理（与 agent_manager 解耦，仅做连接台账，供运维观测）。"""

    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._active.discard(websocket)

    @property
    def active_count(self) -> int:
        return len(self._active)


chat_manager = ChatConnectionManager()


async def _send_error(
    websocket: WebSocket, message: str, code: int | None = None
) -> None:
    payload: dict = {"type": "error", "message": message}
    if code is not None:
        payload["code"] = code
    await websocket.send_json(payload)


def _coerce_int(value: object) -> int | None:
    """容忍 str 数字（query/JSON 混用场景），非法值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


@router.websocket("/ws/chat")
async def chat_endpoint(websocket: WebSocket, token: str = "") -> None:
    """LLM 对话流式通道：握手鉴权 → 循环收消息 → 逐轮推 token/done 事件。"""
    payload = decode_token_payload(token)
    if payload is None or not payload.get("sub") or not payload.get("role"):
        await websocket.close(code=1008)
        return
    user = CurrentUser(username=payload["sub"], role=payload["role"])

    await chat_manager.connect(websocket)
    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001 —— 非 JSON 帧等
                logger.warning(f"/ws/chat 非法上行帧: {type(exc).__name__}")
                await _send_error(websocket, "消息必须是 JSON 对象")
                continue

            if not isinstance(raw, dict):
                await _send_error(websocket, "消息必须是 JSON 对象")
                continue

            project_id = _coerce_int(raw.get("project_id"))
            message = raw.get("message")
            if project_id is None or project_id < 1 or not isinstance(message, str):
                await _send_error(
                    websocket, "参数非法：project_id(int>=1) 与 message(str) 必填"
                )
                continue
            message = message.strip()
            if not message or len(message) > 2000:
                await _send_error(
                    websocket, "参数非法：message 长度需在 1~2000 字符之间"
                )
                continue

            use_tools = raw.get("use_tools", True)
            if not isinstance(use_tools, bool):
                await _send_error(websocket, "参数非法：use_tools 必须为布尔值")
                continue
            top_k = (
                _coerce_int(raw.get("top_k")) if raw.get("top_k") is not None else None
            )
            if top_k is not None and not 1 <= top_k <= 20:
                await _send_error(websocket, "参数非法：top_k 取值范围 1~20")
                continue
            run_no = raw.get("run_no")
            if run_no is not None:
                if not isinstance(run_no, str) or not run_no.strip():
                    await _send_error(websocket, "参数非法：run_no 需为非空字符串")
                    continue
                run_no = run_no.strip()

            # 每轮问答前的多租户门禁（独立 DB 会话，不跨事件持有）
            async with SessionLocal() as db:
                try:
                    await ensure_project_access(db, project_id, user, "viewer")
                    if run_no:
                        await ensure_run_visible(db, run_no, user)
                except BusinessError as exc:
                    # run_no 不存在对齐 SRS 6.3 的 4004；其余门禁错误不透传内部码
                    code = (
                        ERR_METRIC_VALIDATION_FAILED
                        if (run_no and exc.code == 2003)
                        else None
                    )
                    await _send_error(websocket, exc.message, code)
                    await websocket.close(code=1008)
                    return

            try:
                async for event in astream_qa_events(
                    project_id,
                    message,
                    top_k=top_k,
                    use_tools=use_tools,
                    run_no=run_no,
                ):
                    await websocket.send_json(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"/ws/chat 事件流异常: {type(exc).__name__}: {exc}")
                await _send_error(websocket, "服务内部异常，事件流终止")
    except WebSocketDisconnect:
        pass
    finally:
        chat_manager.disconnect(websocket)
