"""c1: test_scenario 新增 project_id（场景归属项目）

Revision id: 20260913c1
Revises: 20260913b1
Create Date: 2026-09-13

变更：
- test_scenario 新增 project_id BIGINT，外键指向 test_project.id（RESTRICT）
- 历史场景无项目归属：确保存在名为「默认项目」的项目（system 创建），
  将 project_id 为空的场景全部回填到该项目，再置 NOT NULL
- project_id 加普通索引（按项目列场景用）

幂等性：DDL 用 information_schema 存在性检查；默认项目用 NOT EXISTS 守卫插入，
支持安全重跑。与 b1（jmeter_script.project_id）同口径。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260913c1"
down_revision: Union[str, None] = "20260913b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PROJECT_NAME = "默认项目"
_FK = "fk_test_scenario_project"
_INDEX = "ix_test_scenario_project_id"


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


def _constraint_exists(table: str, constraint: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = DATABASE() AND table_name = :t "
            "AND constraint_name = :c AND constraint_type = 'FOREIGN KEY'"
        ),
        {"t": table, "c": constraint},
    ).first()
    return row is not None


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
    bind = op.get_bind()

    # 1) 加可空列
    if not _column_exists("test_scenario", "project_id"):
        op.add_column(
            "test_scenario", sa.Column("project_id", sa.BigInteger(), nullable=True)
        )

    # 2) 确保「默认项目」存在（名称唯一约束兜底 + NOT EXISTS 守卫）
    bind.execute(
        sa.text(
            "INSERT INTO test_project (name, description, created_by, created_at, updated_at) "
            "SELECT :n, :d, :u, NOW(), NOW() FROM DUAL "
            "WHERE NOT EXISTS (SELECT 1 FROM test_project WHERE name = :n)"
        ),
        {
            "n": _DEFAULT_PROJECT_NAME,
            "d": "系统迁移自动创建，用于归集历史脚本与场景",
            "u": "system",
        },
    )
    default_project_id = bind.execute(
        sa.text("SELECT id FROM test_project WHERE name = :n"),
        {"n": _DEFAULT_PROJECT_NAME},
    ).scalar_one()

    # 3) 历史场景回填默认项目
    bind.execute(
        sa.text("UPDATE test_scenario SET project_id = :pid WHERE project_id IS NULL"),
        {"pid": default_project_id},
    )

    # 4) 置 NOT NULL + 外键 + 索引
    op.alter_column(
        "test_scenario",
        "project_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    if not _constraint_exists("test_scenario", _FK):
        op.create_foreign_key(
            _FK,
            "test_scenario",
            "test_project",
            ["project_id"],
            ["id"],
        )
    if not _index_exists("test_scenario", _INDEX):
        op.create_index(_INDEX, "test_scenario", ["project_id"])


def downgrade() -> None:
    if _index_exists("test_scenario", _INDEX):
        op.drop_index(_INDEX, table_name="test_scenario")
    if _constraint_exists("test_scenario", _FK):
        op.drop_constraint(_FK, "test_scenario", type_="foreignkey")
    if _column_exists("test_scenario", "project_id"):
        op.drop_column("test_scenario", "project_id")
