"""a1: test_environment 被测环境清单表（P1 结构化资产 A1）

Revision id: 20260916a1
Revises: 20260914f1
Create Date: 2026-09-16

变更：
- 新建 test_environment 表：项目内被测环境资产
  * (project_id, env_code) 项目内唯一：env_code 为机器可读编码
  * project_id 外键指向 test_project.id（RESTRICT：环境删除走业务预检，
    不依赖 DB 级联；项目级联删除在应用层逐行处理，与脚本口径一致）
  * hosts / db_connections / middleware_info / variables 为 JSON 列
- 纯新增表，无存量数据回填，向后兼容

幂等性：建表用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916a1"
down_revision: Union[str, None] = "20260914f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "test_environment"
_UQ = "uq_test_environment_code"
_FK = "fk_test_environment_project"
_INDEX_PROJECT = "ix_test_environment_project_id"


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
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("env_code", sa.String(length=64), nullable=False),
            sa.Column(
                "base_url",
                sa.String(length=512),
                nullable=False,
                server_default="",
            ),
            sa.Column("hosts", sa.JSON(), nullable=True),
            sa.Column("db_connections", sa.JSON(), nullable=True),
            sa.Column("middleware_info", sa.JSON(), nullable=True),
            sa.Column("variables", sa.JSON(), nullable=True),
            sa.Column(
                "description",
                sa.String(length=512),
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
            sa.UniqueConstraint("project_id", "env_code", name=_UQ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["test_project.id"],
                name=_FK,
            ),
        )
    if not _index_exists(_TABLE, _INDEX_PROJECT):
        op.create_index(_INDEX_PROJECT, _TABLE, ["project_id"])


def downgrade() -> None:
    if _index_exists(_TABLE, _INDEX_PROJECT):
        op.drop_index(_INDEX_PROJECT, table_name=_TABLE)
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
