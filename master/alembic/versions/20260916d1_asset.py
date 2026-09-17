"""d1: test_asset 文档资产表（P2 文档资产管道 D1）

Revision id: 20260916d1
Revises: 20260916c1
Create Date: 2026-09-16

新增 test_asset 表：用户上传的测试方案/环境清单/交易清单/SLA/架构文档元数据。
文件本体存 MinIO（key=assets/{asset_id}/{filename}），本表存元数据与解析状态。
(project_id, hash_sha256) 唯一约束：同项目内同内容重复上传复用原 asset_id。

字段：
- project_id FK→test_project（RESTRICT，同 environment/transaction 口径）
- asset_type / status：枚举值字符串（AssetType / AssetStatus）
- filename / file_key / hash_sha256 / file_size / content_type
- description / parse_meta(JSON，D3 写入解析结果) / created_by
- id / created_at / updated_at（Base 公共 Mixin）

幂等性：用 information_schema.tables 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916d1"
down_revision: Union[str, None] = "20260916c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "test_asset"


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


def upgrade() -> None:
    if _table_exists(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("test_project.id", name="fk_test_asset_project"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("filename", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("file_key", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("hash_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "file_size", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "content_type", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column(
            "description", sa.String(length=512), nullable=False, server_default=""
        ),
        sa.Column("parse_meta", sa.JSON(), nullable=True),
        sa.Column(
            "created_by", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("project_id", "hash_sha256", name="uq_test_asset_hash"),
    )
    op.create_index("ix_test_asset_project_id", _TABLE, ["project_id"])
    op.create_index("ix_test_asset_asset_type", _TABLE, ["asset_type"])
    op.create_index("ix_test_asset_status", _TABLE, ["status"])
    op.create_index("ix_test_asset_hash_sha256", _TABLE, ["hash_sha256"])


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
