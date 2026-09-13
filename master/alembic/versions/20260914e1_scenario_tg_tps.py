"""e1: scenario_script_tg 线程组参数 loops 替换为 tps

Revision id: 20260914e1
Revises: 20260913d1
Create Date: 2026-09-14

变更：
- scenario_script_tg 新增 tps 列（INT NOT NULL DEFAULT 0）：目标每秒样本数，
  0 表示不限速；执行期 ×60 换算 TPM 写入常量吞吐量定时器
- 删除 loops 列：循环次数不再可配，非基准场景执行期统一无限循环（场景时长
  收口），单交易基准固定 100 次（jmx_assembler 内常量）

不做数据回填：loops 与 tps 语义不同，旧行统一给默认 0（不限速），
配合 scheduler+duration 执行期无限循环，行为与历史 loops=-1 的定时场景一致。

幂等性：列增删均用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260914e1"
down_revision: Union[str, None] = "20260913d1"
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
    if not _column_exists(_TABLE, "tps"):
        op.add_column(
            _TABLE,
            sa.Column("tps", sa.Integer(), nullable=False, server_default="0"),
        )
    if _column_exists(_TABLE, "loops"):
        op.drop_column(_TABLE, "loops")


def downgrade() -> None:
    if not _column_exists(_TABLE, "loops"):
        op.add_column(
            _TABLE,
            sa.Column("loops", sa.Integer(), nullable=False, server_default="1"),
        )
    if _column_exists(_TABLE, "tps"):
        op.drop_column(_TABLE, "tps")
