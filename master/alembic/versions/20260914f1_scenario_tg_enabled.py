"""f1: scenario_script_tg 新增线程组启用状态 enabled

Revision id: 20260914f1
Revises: 20260914e1
Create Date: 2026-09-14

变更：
- scenario_script_tg 新增 enabled 列（TINYINT(1) NOT NULL DEFAULT 1）：
  线程组场景级启用开关，初始值沿用脚本扫描结果，执行期由 jmx_assembler
  写入线程组节点 enabled 属性，false 时整组不执行。

存量行统一回填为启用（1），与历史行为一致（历史场景全部线程组均执行）。

幂等性：列增删均用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260914f1"
down_revision: Union[str, None] = "20260914e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "scenario_script_tg"


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


def upgrade() -> None:
    if not _column_exists(_TABLE, "enabled"):
        op.add_column(
            _TABLE,
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    if _column_exists(_TABLE, "enabled"):
        op.drop_column(_TABLE, "enabled")
