#!/bin/sh
# master 容器入口：先迁移数据库 schema，再启动 API。
# 必须先于 uvicorn 执行——应用 lifespan 只跑 create_all（仅补建新表，
# 不会给旧表加列），schema 演进统一以 alembic 迁移为准。
# compose 已用 mysql healthcheck 保证本脚本执行时 MySQL 可连；
# 迁移失败则容器退出（set -e），避免带错误 schema 提供服务。
set -e

# 历史包袱：迁移链首个版本 20260908a1（down_revision=None）是增量 ALTER
# （jmeter_script.data_files），并非建表基线——项目早期靠 create_all 建表，
# 迁移只在已存在的表上演进。因此全新空库直接 upgrade head 会在第一个迁移
# 报 1146 "Table doesn't exist"。
# 引导策略（alembic 官方对"无基线迁移史"的标准做法）：
#   - alembic current 无版本行 = 空库/未纳管库 → 按当前模型 create_all
#     建出全量最新 schema，再 stamp head 声明所有迁移已应用；
#   - 已有版本行的库 → 正常 upgrade head 走增量迁移。
if alembic current 2>/dev/null | grep -q .; then
    echo "[entrypoint] 已纳管数据库：alembic upgrade head"
    alembic upgrade head
else
    echo "[entrypoint] 空库（无 alembic 版本）：create_all 建全量基线 → stamp head"
    python - <<'PY'
import asyncio

from app.db.session import engine
from app.models import Base  # noqa: F401  确保模型全部注册进 metadata


async def _bootstrap() -> None:
    # checkfirst=True：已存在的表跳过（不改动结构），只补建缺失表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


asyncio.run(_bootstrap())
PY
    alembic stamp head
fi

exec "$@"
