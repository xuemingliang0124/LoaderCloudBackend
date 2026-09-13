"""d1: project_member 项目成员表（项目级授权基础层 P1）

Revision id: 20260913d1
Revises: 20260913c1
Create Date: 2026-09-13

变更：
- 新建 project_member 表：(project_id, username) 唯一，role ∈ owner/editor/viewer
- project_id 外键指向 test_project.id（CASCADE，删项目连带清成员）
- username 不做物理外键：身份以字符串承载（与 test_project.created_by、
  JWT sub 口径一致，sys_user.username 唯一索引保证其可作逻辑主键）
- 回填（granted_by='system'）：
  * 每个已有项目的创建者（须存在于 sys_user）→ 该项目 owner
  * 全局 admin 用户 → 所有已有项目的 owner

幂等性：建表用 information_schema 存在性检查；回填用 NOT EXISTS 守卫，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260913d1"
down_revision: Union[str, None] = "20260913c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "project_member"
_UQ = "uq_project_member"
_FK = "fk_project_member_project"
_INDEX_PROJECT = "ix_project_member_project_id"
_INDEX_USERNAME = "ix_project_member_username"


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

    # 1) 建表（幂等）
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column(
                "granted_by", sa.String(length=64), nullable=False, server_default=""
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
            sa.UniqueConstraint("project_id", "username", name=_UQ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["test_project.id"],
                name=_FK,
                ondelete="CASCADE",
            ),
        )
    if not _index_exists(_TABLE, _INDEX_PROJECT):
        op.create_index(_INDEX_PROJECT, _TABLE, ["project_id"])
    if not _index_exists(_TABLE, _INDEX_USERNAME):
        op.create_index(_INDEX_USERNAME, _TABLE, ["username"])

    # 2) 回填项目创建者为 owner（须为 sys_user 中真实存在的账号）
    bind.execute(
        sa.text(
            "INSERT INTO project_member "
            "(project_id, username, role, granted_by, created_at, updated_at) "
            "SELECT p.id, p.created_by, 'owner', 'system', NOW(), NOW() "
            "FROM test_project p "
            "WHERE p.created_by <> '' "
            "AND EXISTS (SELECT 1 FROM sys_user u WHERE u.username = p.created_by) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM project_member m "
            "  WHERE m.project_id = p.id AND m.username = p.created_by)"
        )
    )

    # 3) 回填全局 admin 用户为所有项目的 owner
    bind.execute(
        sa.text(
            "INSERT INTO project_member "
            "(project_id, username, role, granted_by, created_at, updated_at) "
            "SELECT p.id, u.username, 'owner', 'system', NOW(), NOW() "
            "FROM test_project p JOIN sys_user u ON u.role = 'admin' "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM project_member m "
            "  WHERE m.project_id = p.id AND m.username = u.username)"
        )
    )


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
