"""日志：统一 loguru，关键路径需带 run_no / agent_id 上下文。

第三方库（uvicorn / sqlalchemy 等）的标准 logging 通过 InterceptHandler
转发到 loguru，实现全应用日志单一出口。DEBUG=true 时：
- loguru stdout 级别降为 DEBUG
- uvicorn 日志级别降为 DEBUG（路由匹配、连接生命周期等）
- sqlalchemy.engine 日志级别降为 INFO（配合 echo=True 输出 SQL 语句）
"""

import logging
import sys

from loguru import logger


class InterceptHandler(logging.Handler):
    """拦截标准 logging 转发到 loguru，统一 uvicorn/sqlalchemy 等第三方日志。"""

    def emit(self, record: logging.LogRecord) -> None:
        # 获取对应的 loguru level
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # 找到真正发出日志的调用栈帧（跳过 logging 内部帧）
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


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

    # 拦截标准 logging → loguru（uvicorn/sqlalchemy 等第三方库日志统一出口）
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # debug 模式下放开 uvicorn 和 sqlalchemy 的日志级别
    uvicorn_level = "DEBUG" if debug else "INFO"
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).setLevel(uvicorn_level)
    # SQLAlchemy echo=True 时 SQL 以 INFO 级输出到 sqlalchemy.engine logger
    logging.getLogger("sqlalchemy.engine").setLevel("INFO" if debug else "WARNING")
