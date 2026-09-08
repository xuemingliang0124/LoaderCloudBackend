"""Agent 入口：装配状态机 / 执行器 / 上报通道并常驻运行。

用法：python -m pt_agent.main
"""

import asyncio
import sys

from loguru import logger

from pt_agent.config import get_settings
from pt_agent.executor import TaskExecutor
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
    state = AgentState()
    runner = JMeterRunner(settings.jmeter_bin, settings.work_dir)
    reporter = Reporter(settings, state)
    executor = TaskExecutor(state, runner, reporter)
    reporter.bind_executor(executor)
    logger.info(
        f"Agent[{settings.resolved_agent_id}] 启动 | Master={settings.master_ws_url} "
        f"| tags={settings.tag_list} | jmeter={settings.jmeter_bin}"
    )
    await reporter.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent 退出")
