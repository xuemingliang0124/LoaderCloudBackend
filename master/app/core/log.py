"""日志：统一 loguru，关键路径需带 run_no / agent_id 上下文。"""

import sys

from loguru import logger


def setup_logging(debug: bool = False) -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        level="DEBUG" if debug else "INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
    )
    logger.add(
        "logs/master_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="14 days",
        level="INFO",
        encoding="utf-8",
    )
