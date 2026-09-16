"""c1: test_scenario.environment_id 场景绑定被测环境（P1 结构化资产 A3）

Revision id: 20260916c1
Revises: 20260916b1
Create Date: 2026-09-16

变更：
- test_scenario 表新增 environment_id 列（nullable + FK 指向 test_environment.id）
  * 弱关联：nullable 兼容存量未绑定环境的场景
  * ondelete=SET NULL：环境删除由删除预检阻断（严格模式 3043），
    force 模式下应用层解绑引用再删环境；DB 层 SET NULL 作为兜底
  * 加索引便于环境删除预检按 environment_id 反查引用场景数
- 纯新增列，无数据回填，向后兼容（存量场景 environment_id 为 NULL）

幂等性：列与索引、外键均用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916c1"
down_revision: Union[str, None] = "20260916b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "test_scenario"
_COLUMN = "environment_id"
_FK = "fk_test_scenario_environment"
_INDEX = "ix_test_scenario_environment_id"


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


def _fk_exists(fk_name: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = DATABASE() "
            "AND constraint_name = :c AND constraint_type = 'FOREIGN KEY'"
        ),
        {"c": fk_name},
    ).first()
    return row is not None


def upgrade() -> None:
    if not _column_exists(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.BigInteger(), nullable=True),
        )
    if not _fk_exists(_FK):
        op.create_foreign_key(
            _FK,
            _TABLE,
            "test_environment",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
    if not _index_exists(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    if _index_exists(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _fk_exists(_FK):
        op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    if _column_exists(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
