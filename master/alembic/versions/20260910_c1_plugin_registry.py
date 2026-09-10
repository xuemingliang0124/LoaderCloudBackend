"""c1 plugin registry: global plugin pool + agent_plugin mapping

Revision id: 20260910c1
Revises: 20260910b1
Create Date: 2026-09-10

设计：
- 新建 jmeter_plugin 表（平台级插件池，sha256 去重）
- 新建 agent_plugin 关联表（Agent 实际安装记录，pending_remove 支持延后清理）
- 历史脚本级插件（jmeter_script.plugins）按 sha256 迁移到全局池
- jmeter_script.plugins 列保留做审计兜底（生产环境建议备份后再删）

幂等性：所有 DDL 用 information_schema 存在性检查，支持安全重跑
（MySQL DDL 自动提交，迁移中途失败也能重入）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260910c1"
down_revision: Union[str, None] = "20260910b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """查 information_schema 判断列是否已存在（幂等加列用）。"""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return row is not None


def _table_exists(table: str) -> bool:
    """查 information_schema 判断表是否已存在（幂等建表用）。"""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": table},
    ).first()
    return row is not None


def _index_exists(index_name: str, table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = :t AND index_name = :i"
        ),
        {"t": table, "i": index_name},
    ).first()
    return row is not None


def upgrade() -> None:
    # 1. 插件注册表（幂等建表）
    if not _table_exists("jmeter_plugin"):
        op.create_table(
            "jmeter_plugin",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "version", sa.String(length=32), nullable=False, server_default="v1"
            ),
            sa.Column(
                "file_key", sa.String(length=256), nullable=False, server_default=""
            ),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")
            ),
            sa.Column(
                "description", sa.String(length=512), nullable=False, server_default=""
            ),
            sa.Column(
                "created_by", sa.String(length=64), nullable=False, server_default=""
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("name", "version", name="uq_plugin_name_version"),
        )
        op.create_index("ix_jmeter_plugin_name", "jmeter_plugin", ["name"])
        op.create_index("ix_jmeter_plugin_sha256", "jmeter_plugin", ["sha256"])

    # 2. Agent-Plugin 关联表（幂等建表）
    if not _table_exists("agent_plugin"):
        op.create_table(
            "agent_plugin",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("plugin_id", sa.BigInteger(), nullable=False),
            sa.Column(
                "installed_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "installed_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="installed",
            ),
            sa.UniqueConstraint("agent_id", "plugin_id", name="uq_agent_plugin"),
        )
        # 外键单独加（建表时带 FK 会让幂等重跑复杂）
        op.create_foreign_key(
            "fk_agent_plugin_agent_id",
            "agent_plugin",
            "agent_node",
            ["agent_id"],
            ["agent_id"],
        )
        op.create_foreign_key(
            "fk_agent_plugin_plugin_id",
            "agent_plugin",
            "jmeter_plugin",
            ["plugin_id"],
            ["id"],
        )
        op.create_index("ix_agent_plugin_agent_id", "agent_plugin", ["agent_id"])

    # 3. 历史脚本级插件迁移到全局插件池
    #    jmeter_script.plugins JSON 数组：[{"key": "...", "filename": "..."}]
    #    此处仅建表结构与索引，数据迁移在应用层 service 启动时按 sha256 拉取
    #    去重写入（迁移逻辑复杂、依赖 MinIO 计算 sha256，DDL 阶段不做）
    #    应用层 plugin_sync.recover_legacy_script_plugins() 一次性扫描：
    #      - 对每个 script.plugins 项从 MinIO 下载 jar，算 sha256
    #      - 按 sha256 查 jmeter_plugin，命中则复用对象，未命中则建条目
    #      - 不删除 jmeter_script.plugins 字段（保留作历史审计）

    # 4. AgentNode.plugins 字段语义变更（JSON 数组结构变）但类型不变，无需 DDL
    #    应用层从 ["x.jar"] 改为存 [{"name":"x.jar","sha256":"..."}]，平滑过渡
    #    权威数据迁到 agent_plugin 表，本字段作 Agent 上报快照冗余


def downgrade() -> None:
    if _table_exists("agent_plugin"):
        if _index_exists("ix_agent_plugin_agent_id", "agent_plugin"):
            op.drop_index("ix_agent_plugin_agent_id", table_name="agent_plugin")
        op.drop_table("agent_plugin")
    if _table_exists("jmeter_plugin"):
        if _index_exists("ix_jmeter_plugin_sha256", "jmeter_plugin"):
            op.drop_index("ix_jmeter_plugin_sha256", table_name="jmeter_plugin")
        if _index_exists("ix_jmeter_plugin_name", "jmeter_plugin"):
            op.drop_index("ix_jmeter_plugin_name", table_name="jmeter_plugin")
        op.drop_table("jmeter_plugin")
