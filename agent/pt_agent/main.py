"""Agent 入口：装配状态机 / 执行器 / 上报通道并常驻运行。

用法：python -m pt_agent.main
"""

import asyncio
import sys

from loguru import logger

from pt_agent.collector import snapshot
from pt_agent.config import get_settings
from pt_agent.executor import TaskExecutor
from pt_agent.identity import resolve_identity
from pt_agent.plugin_sync import PluginSyncer
from pt_agent.plugins import scan_plugins
from pt_agent.reporter import Reporter
from pt_agent.runner import JMeterRunner
from pt_agent.state import AgentState


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(
        "logs/agent_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        level="INFO",
        encoding="utf-8",
    )


async def main() -> None:
    setup_logging()
    settings = get_settings()
    # 本机规格 + 已装插件清单（注册时上报，供选机与插件校验）
    caps = snapshot()
    plugins = scan_plugins(settings.jmeter_bin, settings.plugin_dir_path)
    # 先向 Master 注册换取固定 agent_id（内部重试，拿到 ID 后才继续）
    # register 响应附 expected_plugins，Agent 据此对齐 plugin_dir
    identity = await resolve_identity(
        settings,
        plugins=plugins,
        cpu_cores=int(caps.get("cpu_cores") or 0),
        mem_total_gb=float(caps.get("mem_total_gb") or 0.0),
    )
    settings.agent_id = identity.agent_id

    state = AgentState()
    runner = JMeterRunner(settings.jmeter_bin, settings.work_dir)
    reporter = Reporter(
        settings,
        state,
        ip=identity.ip,
        hostname=identity.hostname,
        cpu_cores=int(caps.get("cpu_cores") or 0),
        mem_total_gb=float(caps.get("mem_total_gb") or 0.0),
    )
    plugin_syncer = PluginSyncer(settings, reporter)
    reporter.bind_plugin_syncer(plugin_syncer)
    # 启动时按 expected_plugins 对齐 plugin_dir（缺失下载、多余删除）
    if identity.expected_plugins is not None:
        await plugin_syncer.sync_from_expected(identity.expected_plugins)

    executor = TaskExecutor(state, runner, reporter)
    reporter.bind_executor(executor)
    logger.info(
        f"Agent[{settings.resolved_agent_id}] 启动 | Master={settings.master_ws_url} "
        f"| ip={identity.ip} | tags={settings.tag_list} | jmeter={settings.jmeter_bin} "
        f"| cores={caps.get('cpu_cores')} | plugins={len(plugins)}"
    )
    await reporter.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent 退出")
