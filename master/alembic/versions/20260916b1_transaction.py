"""b1: test_transaction 被测交易清单表（P1 结构化资产 A2）

Revision id: 20260916b1
Revises: 20260916a1
Create Date: 2026-09-16

变更：
- 新建 test_transaction 表：项目内被测交易资产（登录/下单/支付等业务动作）
  * (project_id, txn_code) 项目内唯一：txn_code 为机器可读编码
  * project_id 外键指向 test_project.id（RESTRICT：交易删除走业务预检，
    不依赖 DB 级联；项目级联删除在应用层逐行处理，与环境/脚本口径一致）
  * default_script_id 弱关联指向 jmeter_script.id（nullable，ondelete=SET NULL，
    仅标记默认执行版本，不阻断脚本删除：脚本删除时由 DB 自动置空）
  * sla_tps / sla_error_rate 用 FLOAT（监控阈值非货币，跨库行为一致）
- 纯新增表，无存量数据回填，向后兼容

幂等性：建表与索引均用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916b1"
down_revision: Union[str, None] = "20260916a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "test_transaction"
_UQ = "uq_test_transaction_code"
_FK_PROJECT = "fk_test_transaction_project"
_FK_SCRIPT = "fk_test_transaction_default_script"
_INDEX_PROJECT = "ix_test_transaction_project_id"
_INDEX_SCRIPT = "ix_test_transaction_default_script_id"


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
            sa.Column("txn_code", sa.String(length=64), nullable=False),
            sa.Column("default_script_id", sa.BigInteger(), nullable=True),
            sa.Column("sla_tps", sa.Float(), nullable=True),
            sa.Column("sla_p95_ms", sa.Integer(), nullable=True),
            sa.Column("sla_error_rate", sa.Float(), nullable=True),
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
            sa.UniqueConstraint("project_id", "txn_code", name=_UQ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["test_project.id"],
                name=_FK_PROJECT,
            ),
            sa.ForeignKeyConstraint(
                ["default_script_id"],
                ["jmeter_script.id"],
                name=_FK_SCRIPT,
                ondelete="SET NULL",
            ),
        )
    if not _index_exists(_TABLE, _INDEX_PROJECT):
        op.create_index(_INDEX_PROJECT, _TABLE, ["project_id"])
    if not _index_exists(_TABLE, _INDEX_SCRIPT):
        op.create_index(_INDEX_SCRIPT, _TABLE, ["default_script_id"])


def downgrade() -> None:
    if _index_exists(_TABLE, _INDEX_SCRIPT):
        op.drop_index(_INDEX_SCRIPT, table_name=_TABLE)
    if _index_exists(_TABLE, _INDEX_PROJECT):
        op.drop_index(_INDEX_PROJECT, table_name=_TABLE)
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
