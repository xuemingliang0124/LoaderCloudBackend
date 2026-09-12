"""f1: scenario_type 收敛为 4 类枚举（单交易基准/单交易负载/混合场景/稳定性）

Revision id: 20260912f1
Revises: 20260912e1

说明：
- 列保持 VARCHAR(32)（SAEnum native_enum=False，与 RunStatus 存储口径一致）
- DB 存枚举 name（SINGLE_BASELINE 等），ORM 校验取值，API 层输出中文 value
- 回填历史空串/非法值为 SINGLE_BASELINE（单交易基准），并同步 server_default
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260912f1"
down_revision: Union[str, None] = "20260912e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALID = ("SINGLE_BASELINE", "SINGLE_LOAD", "MIXED", "STABILITY")


def upgrade() -> None:
    bind = op.get_bind()
    placeholders = ", ".join(f":v{i}" for i in range(len(_VALID)))
    params = {f"v{i}": v for i, v in enumerate(_VALID)}
    params["default"] = "SINGLE_BASELINE"
    bind.execute(
        sa.text(
            f"UPDATE test_scenario SET scenario_type = :default "
            f"WHERE scenario_type IS NULL OR scenario_type NOT IN ({placeholders})"
        ),
        params,
    )
    op.alter_column(
        "test_scenario",
        "scenario_type",
        existing_type=sa.String(length=32),
        server_default="SINGLE_BASELINE",
    )


def downgrade() -> None:
    op.alter_column(
        "test_scenario",
        "scenario_type",
        existing_type=sa.String(length=32),
        server_default="",
    )
    op.get_bind().execute(sa.text("UPDATE test_scenario SET scenario_type = ''"))
