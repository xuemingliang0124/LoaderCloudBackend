#!/bin/sh
# master 容器入口：先迁移数据库 schema，再启动 API。
# 必须先于 uvicorn 执行——应用 lifespan 只跑 create_all（仅补建新表，
# 不会给旧表加列），schema 演进统一以 alembic 迁移为准。
# compose 已用 mysql healthcheck 保证本脚本执行时 MySQL 可连；
# 迁移失败则容器退出（set -e），避免带错误 schema 提供服务。
set -e

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

exec "$@"
