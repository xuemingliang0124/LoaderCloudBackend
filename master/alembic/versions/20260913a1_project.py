"""a1: 新建项目管理模块 test_project 表

Revision id: 20260913a1
Revises: 20260912f1
Create Date: 2026-09-13

变更：
- 新建 test_project：项目基础信息（名称唯一、描述、创建人）+ 公共时间戳
- name 普通索引 + 唯一约束（与 test_scenario 口径一致）

幂等性：用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260913a1"
down_revision: Union[str, None] = "20260912f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    if not _table_exists("test_project"):
        op.create_table(
            "test_project",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column(
                "description",
                sa.String(length=512),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "created_by",
                sa.String(length=64),
                nullable=False,
                server_default="",
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
            sa.UniqueConstraint("name", name="uq_test_project_name"),
        )
        op.create_index("ix_test_project_name", "test_project", ["name"])


def downgrade() -> None:
    if _table_exists("test_project"):
        if _index_exists("test_project", "ix_test_project_name"):
            op.drop_index("ix_test_project_name", table_name="test_project")
        op.drop_table("test_project")
