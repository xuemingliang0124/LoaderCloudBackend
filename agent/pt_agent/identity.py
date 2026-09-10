"""Agent 身份解析：探测宿主机 IP，向 Master 注册/查询固定 agent_id。

流程：启动探测到达 Master 的本机 IP → HTTP POST /api/v1/agents/register
→ Master 按 IP 查库：已存在返回原 agent_id，不存在则建号。Master 不可达时
指数退避重试（与 WS 重连同策略），拿到 ID 前不进入后续流程。
register 响应附 expected_plugins，Agent 据此对齐 plugin_dir。
"""

import asyncio
import platform
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from loguru import logger

from pt_agent.config import AgentSettings


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    ip: str
    hostname: str
    # Master 期望 Agent 安装的插件清单（含预签 URL），启动时对齐 plugin_dir 用
    expected_plugins: list[dict] = field(default_factory=list)


def detect_host_ip(master_ws_url: str) -> str:
    """探测到达 Master 所用的本机 IP（UDP connect 技巧，不发实际报文）。

    多网卡机器上自动选中"有路由到 Master"的网卡；失败兜底主机名解析。
    阻塞调用，调用方需用 asyncio.to_thread 包装。
    """
    parsed = urlparse(master_ws_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


async def resolve_identity(
    settings: AgentSettings,
    plugins: list[dict] | None = None,
    cpu_cores: int = 0,
    mem_total_gb: float = 0.0,
) -> AgentIdentity:
    """解析 Agent 固定身份：显式 AGENT_ID 优先；否则向 Master 按 IP 注册/查询。"""
    hostname = platform.node()
    ip = await asyncio.to_thread(detect_host_ip, settings.master_ws_url)

    if settings.agent_id:
        # 手动指定 ID 时跳过注册（WS 握手的 upsert 会补建/刷新记录）
        # expected_plugins 缺省为空，Agent 不会主动拉取（仅靠在线推送）
        logger.info(f"使用手动配置 agent_id={settings.agent_id} ip={ip}")
        return AgentIdentity(agent_id=settings.agent_id, ip=ip, hostname=hostname)

    url = f"{settings.master_http_base}/api/v1/agents/register"
    payload = {
        "ip": ip,
        "hostname": hostname,
        "tags": settings.tag_list,
        "plugins": plugins or [],
        "cpu_cores": cpu_cores,
        "mem_total_gb": mem_total_gb,
    }
    delay = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                body = resp.json()
            if body.get("code") != 0:
                raise RuntimeError(body.get("message") or "注册失败")
            data = body["data"]
            logger.info(
                f"Agent 身份确认: id={data['agent_id']} ip={ip} "
                f"hostname={hostname} is_new={data.get('is_new')} "
                f"expected_plugins={len(data.get('expected_plugins') or [])}"
            )
            return AgentIdentity(
                agent_id=data["agent_id"],
                ip=ip,
                hostname=hostname,
                expected_plugins=data.get("expected_plugins") or [],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"向 Master 注册失败: {exc}，{delay:.0f}s 后重试")
            await asyncio.sleep(delay)
            delay = min(delay * 2, settings.reconnect_delay_max)
