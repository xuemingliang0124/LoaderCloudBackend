"""压力机管理：注册（Agent 启动调用）+ 列表查询。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.enums import AgentStatus
from app.schemas import AgentOut, AgentRegisterIn, AgentRegisterOut
from app.schemas.common import ok
from app.services import agent_registry
from app.services.exceptions import BusinessError
from app.services.plugin_sync import expected_plugins_response

router = APIRouter()


@router.post("/agents/register")
async def register_agent(payload: AgentRegisterIn) -> dict:
    """Agent 启动注册：按宿主机 IP 换取固定 agent_id（公开端点，Agent 无 JWT）。

    首次上报该 IP 时 Master 自动建号；后续启动返回同一 agent_id。
    响应附 expected_plugins 清单（全局 enabled 插件 + 预签 URL），
    Agent 据此 diff 下载对齐 plugin_dir。
    """
    if not payload.ip:
        raise BusinessError("Agent 宿主机 IP 为空，无法注册", code=3101)
    node, is_new = await agent_registry.register_by_ip(
        ip=payload.ip,
        hostname=payload.hostname,
        tags=payload.tags,
        jmeter_version=payload.jmeter_version,
        plugins=payload.plugins,
        cpu_cores=payload.cpu_cores,
        mem_total_gb=payload.mem_total_gb,
    )
    # 构造 expected_plugins：Agent 据此对齐 plugin_dir
    expected = await expected_plugins_response()
    return ok(
        AgentRegisterOut(
            agent_id=node.agent_id,
            ip=node.ip,
            hostname=node.hostname,
            is_new=is_new,
            expected_plugins=expected,
        ).model_dump()
    )


@router.get("/agents")
async def list_agents(
    keyword: str | None = Query(
        default=None, description="模糊匹配 agent_id/IP/主机名"
    ),
    status: AgentStatus | None = Query(default=None, description="按在线状态精确过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> dict:
    nodes, total = await agent_registry.list_agents(
        db,
        keyword=keyword,
        status=status,
        page=page,
        page_size=page_size,
    )
    items = [AgentOut.model_validate(n).model_dump(mode="json") for n in nodes]
    return ok({"total": total, "items": items})
