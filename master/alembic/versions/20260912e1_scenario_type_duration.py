"""e1: test_scenario 新增 scenario_type / duration 字段

Revision id: 20260912e1
Revises: 20260910d1
Create Date: 2026-09-12

变更：
- test_scenario 新增 scenario_type (VARCHAR(32), 默认空串)：场景分类
- test_scenario 新增 duration (INT, 默认0)：场景级运行时间（秒）

幂等性：用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260912e1"
down_revision: Union[str, None] = "20260910d1"
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


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing(
        "test_scenario",
        sa.Column(
            "scenario_type",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
    )
    _add_column_if_missing(
        "test_scenario",
        sa.Column("duration", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if _column_exists("test_scenario", "duration"):
        op.drop_column("test_scenario", "duration")
    if _column_exists("test_scenario", "scenario_type"):
        op.drop_column("test_scenario", "scenario_type")
