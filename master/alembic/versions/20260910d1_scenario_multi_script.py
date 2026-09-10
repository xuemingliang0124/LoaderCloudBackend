"""d1 scenario multi-script: 场景多脚本组合 + 线程组级设置

Revision id: 20260910d1
Revises: 20260910c1
Create Date: 2026-09-10

变更：
- test_scenario.name 加唯一约束（场景名称不可重复；历史重名数据非破坏式
  追加 "-<id>" 后缀，不删除记录，避免 scenario_run 外键冲突）
- test_scenario 移除 script_id / total_threads / duration / agent_tags /
  agent_count（脚本关联与线程组设置迁移到 scenario_script / scenario_script_tg 表）
- 新建 scenario_script（场景-脚本关联，含顺序与脚本级 agent_tags/agent_count）
- 新建 scenario_script_tg（场景内每个线程组的加压参数）
- run_agent_result 加 scenario_script_id 并把唯一约束改为
  (run_no, agent_id, scenario_script_id)，支持同 Agent 多脚本结果
- scenario_run 加 expected_results（预期结果数）

幂等性：所有 DDL 用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260910d1"
down_revision: Union[str, None] = "20260910c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
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


def _drop_legacy_fks(table: str, columns: tuple[str, ...]) -> None:
    """删除指定列上的遗留外键（MySQL 8 要求先删 FK 再删列，error 1828）。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE table_schema = DATABASE() AND table_name = :t "
            "AND column_name IN :cols AND referenced_table_name IS NOT NULL"
        ).bindparams(sa.bindparam("cols", expanding=True)),
        {"t": table, "cols": list(columns)},
    ).fetchall()
    for (constraint_name,) in rows:
        op.drop_constraint(constraint_name, table, type_="foreignkey")


def _index_exists(table: str, index_name: str) -> bool:
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
    # ---- test_scenario 字段调整 ----
    # 名称加唯一约束：历史重名数据不能直接删除（scenario_run 等外键会阻止，
    # 且历史记录需保留），改为对非最小 id 的重名场景追加 "-<id>" 后缀
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE test_scenario t "
            "JOIN (SELECT name, MIN(id) AS min_id FROM test_scenario "
            "GROUP BY name HAVING COUNT(*) > 1) d "
            "ON t.name = d.name AND t.id <> d.min_id "
            "SET t.name = CONCAT(t.name, '-', t.id)"
        )
    )
    if not _index_exists("test_scenario", "uq_test_scenario_name"):
        op.create_unique_constraint("uq_test_scenario_name", "test_scenario", ["name"])

    # 移除已迁移到关联表的字段（压力机选择也下放到脚本级）
    # MySQL 8 必须先删这些列上的遗留外键（如 test_scenario_ibfk_1）再删列
    _drop_legacy_fks(
        "test_scenario",
        ("script_id", "total_threads", "duration", "agent_tags", "agent_count"),
    )
    for col in ("script_id", "total_threads", "duration", "agent_tags", "agent_count"):
        if _column_exists("test_scenario", col):
            op.drop_column("test_scenario", col)

    # ---- 新建 scenario_script ----
    if not _table_exists("scenario_script"):
        op.create_table(
            "scenario_script",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("scenario_id", sa.BigInteger(), nullable=False),
            sa.Column("script_id", sa.BigInteger(), nullable=False),
            sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("agent_tags", sa.JSON(), nullable=True),
            sa.Column("agent_count", sa.Integer(), nullable=False, server_default="1"),
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
            sa.ForeignKeyConstraint(
                ["scenario_id"],
                ["test_scenario.id"],
                name="fk_scenario_script_scenario",
            ),
            sa.ForeignKeyConstraint(
                ["script_id"], ["jmeter_script.id"], name="fk_scenario_script_script"
            ),
            sa.UniqueConstraint("scenario_id", "script_id", name="uq_scenario_script"),
        )
        op.create_index(
            "ix_scenario_script_scenario_id", "scenario_script", ["scenario_id"]
        )

    # 兼容 scenario_script 已由旧版迁移创建但缺少 agent_tags/agent_count 的情况
    _add_column_if_missing(
        "scenario_script", sa.Column("agent_tags", sa.JSON(), nullable=True)
    )
    _add_column_if_missing(
        "scenario_script",
        sa.Column("agent_count", sa.Integer(), nullable=False, server_default="1"),
    )

    # ---- 新建 scenario_script_tg ----
    if not _table_exists("scenario_script_tg"):
        op.create_table(
            "scenario_script_tg",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("scenario_script_id", sa.BigInteger(), nullable=False),
            sa.Column("thread_group_name", sa.String(length=128), nullable=False),
            sa.Column(
                "testclass",
                sa.String(length=64),
                nullable=False,
                server_default="ThreadGroup",
            ),
            sa.Column("num_threads", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("ramp_time", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("loops", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "scheduler",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("duration", sa.Integer(), nullable=False, server_default="0"),
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
            sa.ForeignKeyConstraint(
                ["scenario_script_id"],
                ["scenario_script.id"],
                name="fk_scenario_script_tg_parent",
            ),
            sa.UniqueConstraint(
                "scenario_script_id",
                "thread_group_name",
                name="uq_scenario_script_tg",
            ),
        )
        op.create_index(
            "ix_scenario_script_tg_parent",
            "scenario_script_tg",
            ["scenario_script_id"],
        )

    # ---- scenario_run 记录预期结果数 ----
    _add_column_if_missing(
        "scenario_run",
        sa.Column("expected_results", sa.Integer(), nullable=False, server_default="0"),
    )

    # ---- run_agent_result 支持同 Agent 多脚本结果 ----
    _add_column_if_missing(
        "run_agent_result",
        sa.Column("scenario_script_id", sa.BigInteger(), nullable=True),
    )
    # 替换唯一约束：(run_no, agent_id) -> (run_no, agent_id, scenario_script_id)
    if _index_exists("run_agent_result", "uq_run_agent_result"):
        op.drop_constraint("uq_run_agent_result", "run_agent_result", type_="unique")
    if not _index_exists("run_agent_result", "uq_run_agent_result"):
        op.create_unique_constraint(
            "uq_run_agent_result",
            "run_agent_result",
            ["run_no", "agent_id", "scenario_script_id"],
        )


def downgrade() -> None:
    if _table_exists("scenario_script_tg"):
        op.drop_index("ix_scenario_script_tg_parent", table_name="scenario_script_tg")
        op.drop_table("scenario_script_tg")
    if _table_exists("scenario_script"):
        op.drop_index("ix_scenario_script_scenario_id", table_name="scenario_script")
        op.drop_table("scenario_script")

    # 恢复 test_scenario 字段（无法恢复历史数据，给默认值）
    if not _column_exists("test_scenario", "script_id"):
        op.add_column(
            "test_scenario",
            sa.Column("script_id", sa.BigInteger(), nullable=True),
        )
    if not _column_exists("test_scenario", "total_threads"):
        op.add_column(
            "test_scenario",
            sa.Column(
                "total_threads", sa.Integer(), nullable=False, server_default="0"
            ),
        )
    if not _column_exists("test_scenario", "duration"):
        op.add_column(
            "test_scenario",
            sa.Column("duration", sa.Integer(), nullable=False, server_default="300"),
        )
    if not _column_exists("test_scenario", "agent_tags"):
        op.add_column(
            "test_scenario", sa.Column("agent_tags", sa.JSON(), nullable=True)
        )
    if not _column_exists("test_scenario", "agent_count"):
        op.add_column(
            "test_scenario",
            sa.Column("agent_count", sa.Integer(), nullable=False, server_default="1"),
        )
    if _index_exists("test_scenario", "uq_test_scenario_name"):
        op.drop_constraint("uq_test_scenario_name", "test_scenario", type_="unique")

    # 恢复 run_agent_result 旧唯一约束并移除 scenario_script_id
    if _index_exists("run_agent_result", "uq_run_agent_result"):
        op.drop_constraint("uq_run_agent_result", "run_agent_result", type_="unique")
    if _column_exists("run_agent_result", "scenario_script_id"):
        op.drop_column("run_agent_result", "scenario_script_id")
    if not _index_exists("run_agent_result", "uq_run_agent_result"):
        op.create_unique_constraint(
            "uq_run_agent_result", "run_agent_result", ["run_no", "agent_id"]
        )
    if _column_exists("scenario_run", "expected_results"):
        op.drop_column("scenario_run", "expected_results")
