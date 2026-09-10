"""p2 production: agent plugins/spec, script plugins, scenario total_threads, run_agent_result

Revision id: 20260910b1
Revises: 20260910a1
Create Date: 2026-09-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260910b1"
down_revision: Union[str, None] = "20260910a1"
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


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    # scenario_run.status 原为 MySQL 本机 ENUM，P2 新增 STOPPING 后枚举值不足，
    # 改为 VARCHAR(16)：后续新增状态无需再 ALTER，且避免 ENUM 不识别值报 1265
    op.execute("ALTER TABLE scenario_run MODIFY COLUMN status VARCHAR(16) NULL")

    # 压力机：已装插件清单 + 规格（核数/内存），用于插件校验与线程按规格拆分
    _add_column_if_missing("agent_node", sa.Column("plugins", sa.JSON(), nullable=True))
    _add_column_if_missing(
        "agent_node",
        sa.Column("cpu_cores", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_if_missing(
        "agent_node",
        sa.Column("mem_total_gb", sa.Float(), nullable=False, server_default="0"),
    )
    # 脚本：第三方插件依赖 [{"key","filename"}]
    _add_column_if_missing(
        "jmeter_script", sa.Column("plugins", sa.JSON(), nullable=True)
    )
    # 场景：总线程数（>0 时按 agent 规格拆分）
    _add_column_if_missing(
        "test_scenario",
        sa.Column("total_threads", sa.Integer(), nullable=False, server_default="0"),
    )
    # 结果汇聚持久化表（替代内存态 _pending_results，支持 Master 重启恢复）
    if not _table_exists("run_agent_result"):
        op.create_table(
            "run_agent_result",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("run_no", sa.String(length=64), nullable=False),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=True),
            sa.Column("artifacts", sa.JSON(), nullable=True),
            sa.Column(
                "failed", sa.Boolean(), nullable=False, server_default=sa.text("0")
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
            sa.UniqueConstraint("run_no", "agent_id", name="uq_run_agent_result"),
        )
        op.create_index("ix_run_agent_result_run_no", "run_agent_result", ["run_no"])


def downgrade() -> None:
    if _table_exists("run_agent_result"):
        op.drop_index("ix_run_agent_result_run_no", table_name="run_agent_result")
        op.drop_table("run_agent_result")
    for table, col in [
        ("test_scenario", "total_threads"),
        ("jmeter_script", "plugins"),
        ("agent_node", "mem_total_gb"),
        ("agent_node", "cpu_cores"),
        ("agent_node", "plugins"),
    ]:
        if _column_exists(table, col):
            op.drop_column(table, col)
