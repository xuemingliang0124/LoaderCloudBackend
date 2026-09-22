"""LLM 对话入口（FR-10，SRS 6.1）。

- POST /chat：同步问答，返回统一包裹 data=AnswerOut（FR-09 五字段）
- POST /chat/stream：SSE 流式问答，事件流 token/error/done（SRS 6.2）
- POST /reports/generate：Stage 4 最小占位（路由 + run 可见性门禁 +
  SRS 响应壳）；真正的 LLM Markdown 报告由 Stage 5 report_generator
  生成并写入 MinIO（实施方案 Stage 5 修改清单）

门禁与降级：
- 项目 viewer+ 才能对话（多租户隔离，复用 ensure_project_access）
- 请求携带 run_no 时先做执行可见性校验：不存在 → 4004（SRS 6.3
  "指标校验失败（run_no 不存在）"），无权 → 3030
- 检索/LLM 基础设施异常：同步接口 503 + 降级 JSON（错误码 4000，
  SRS FR-10"LLM 服务不可用 → 503 并返回降级 JSON"）；流式接口不阻断
  握手，以 error 事件 + done（兜底 AnswerOutput）收尾（NFR-02）
"""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    ensure_project_access,
    ensure_run_visible,
    get_current_user,
)
from app.core.config import ERR_LLM_CALL_FAILED, ERR_METRIC_VALIDATION_FAILED
from app.db.session import SessionLocal, get_db
from app.schemas import AnswerOut, ChatRequestIn, ReportGenerateIn, ReportGenerateOut
from app.schemas.common import ok
from app.services.exceptions import BusinessError
from app.services.llm.client import AnswerOutput
from app.services.llm.orchestrator import astream_qa_events, run_qa_chain

router = APIRouter()


async def _ensure_run_for_chat(
    db: AsyncSession, run_no: str, user: CurrentUser
) -> None:
    """对话携带 run_no 时的执行可见性门禁：不存在映射为 4004，其余原样抛出。"""
    try:
        await ensure_run_visible(db, run_no, user)
    except BusinessError as exc:
        if exc.code == 2003:
            raise BusinessError(
                f"指标校验失败，run_no 不存在: {run_no}",
                code=ERR_METRIC_VALIDATION_FAILED,
            ) from exc
        raise


@router.post("/chat")
async def chat(
    payload: ChatRequestIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """同步对话问答（viewer+）：返回 FR-09 统一 AnswerOut（包裹在 data 内）。"""
    await ensure_project_access(db, payload.project_id, user, "viewer")
    if payload.run_no:
        await _ensure_run_for_chat(db, payload.run_no, user)

    try:
        result = await run_qa_chain(
            payload.project_id,
            payload.message,
            top_k=payload.top_k,
            use_tools=payload.use_tools,
            run_no=payload.run_no,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"同步问答链路不可用: {type(exc).__name__}: {exc}")
        degraded = AnswerOutput(
            answer="（降级回答）LLM 服务暂时不可用，请稍后重试或联系管理员。",
            notes=f"LLM 服务不可用: {type(exc).__name__}",
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": ERR_LLM_CALL_FAILED,
                "message": "LLM 服务不可用，已返回降级 JSON",
                "data": degraded.model_dump(),
            },
        )
    return ok(AnswerOut(**result.model_dump()).model_dump())


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequestIn,
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 流式问答（viewer+）：data: {token/error/done 事件}\\n\\n。

    项目/执行门禁在返回流之前用独立 DB 会话完成，避免流式生成期间长期
    持有请求级会话；事件生成器自身不触库（检索与 LLM 异常已在编排层
    降级为 error+done 事件，不中断 SSE 连接）。
    """
    async with SessionLocal() as db:
        await ensure_project_access(db, payload.project_id, user, "viewer")
        if payload.run_no:
            await _ensure_run_for_chat(db, payload.run_no, user)

    async def event_source():
        try:
            async for event in astream_qa_events(
                payload.project_id,
                payload.message,
                top_k=payload.top_k,
                use_tools=payload.use_tools,
                run_no=payload.run_no,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"SSE 事件流异常: {type(exc).__name__}: {exc}")
            err = {"type": "error", "message": "服务内部异常，事件流终止"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/reports/generate")
async def generate_report(
    payload: ReportGenerateIn,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """为指定执行生成 LLM 报告并写入 MinIO（SRS 6.1）。

    Stage 5 实现：调 report_generator.generate_report 做 DB+ES 聚合 →
    LLM/模板 → MinIO；LLM 不可用时降级模板生成，MinIO 异常不阻断响应。
    """
    await ensure_run_visible(db, payload.run_no, user)
    from app.services.llm.report_generator import generate_report as _generate

    result = await _generate(payload.run_no, db)
    return ok(ReportGenerateOut(**result).model_dump())
