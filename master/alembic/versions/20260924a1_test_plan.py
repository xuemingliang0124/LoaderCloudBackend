"""a1: test_plan / test_plan_scenario 测试方案表（P1 结构化资产 A4）

Revision id: 20260924a1
Revises: 20260916d1
Create Date: 2026-09-24

变更：
- 新建 test_plan 表：项目内测试方案（多场景编排单元 + 整体通过判据）
  * (project_id, name) 项目内唯一：方案名称即业务标识
  * project_id 外键指向 test_project.id（RESTRICT：方案删除走业务预检，
    不依赖 DB 级联；项目级联删除在应用层 bulk delete，与环境/交易/资产口径一致）
  * pass_criteria JSON 自由结构（如 {"max_p95_ms": 500}，由报告/LLM 模块解释）
- 新建 test_plan_scenario 弱关联表：方案挂载场景（含 seq 执行顺序 / weight 权重）
  * (plan_id, scenario_id) 唯一：同一场景在同一方案内不可重复挂载
  * plan_id / scenario_id FK 均默认 RESTRICT：不级联删除场景
    （保持 Scenario 可独立执行）；方案删除由 ORM delete-orphan 级联关联行，
    项目 force 删除在应用层 bulk delete 关联行；场景删除走预检（严格 3017
    阻断，force 应用层解绑）
- 纯新增表，无存量数据回填，向后兼容

幂等性：建表与索引均用 information_schema 存在性检查，支持安全重跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260924a1"
down_revision: Union[str, None] = "20260916d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE_PLAN = "test_plan"
_TABLE_PS = "test_plan_scenario"
_UQ_PLAN = "uq_test_plan_name"
_UQ_PS = "uq_test_plan_scenario"
_FK_PLAN_PROJECT = "fk_test_plan_project"
_FK_PS_PLAN = "fk_test_plan_scenario_plan"
_FK_PS_SCENARIO = "fk_test_plan_scenario_scenario"
_INDEX_PLAN_PROJECT = "ix_test_plan_project_id"
_INDEX_PS_PLAN = "ix_test_plan_scenario_plan_id"
_INDEX_PS_SCENARIO = "ix_test_plan_scenario_scenario_id"


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
    if not _table_exists(_TABLE_PLAN):
        op.create_table(
            _TABLE_PLAN,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("pass_criteria", sa.JSON(), nullable=True),
            sa.Column(
                "report_template",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            ),
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
            sa.UniqueConstraint("project_id", "name", name=_UQ_PLAN),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["test_project.id"],
                name=_FK_PLAN_PROJECT,
            ),
        )
    if not _index_exists(_TABLE_PLAN, _INDEX_PLAN_PROJECT):
        op.create_index(_INDEX_PLAN_PROJECT, _TABLE_PLAN, ["project_id"])

    if not _table_exists(_TABLE_PS):
        op.create_table(
            _TABLE_PS,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("plan_id", sa.BigInteger(), nullable=False),
            sa.Column("scenario_id", sa.BigInteger(), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
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
            sa.UniqueConstraint("plan_id", "scenario_id", name=_UQ_PS),
            sa.ForeignKeyConstraint(
                ["plan_id"],
                ["test_plan.id"],
                name=_FK_PS_PLAN,
            ),
            sa.ForeignKeyConstraint(
                ["scenario_id"],
                ["test_scenario.id"],
                name=_FK_PS_SCENARIO,
            ),
        )
    if not _index_exists(_TABLE_PS, _INDEX_PS_PLAN):
        op.create_index(_INDEX_PS_PLAN, _TABLE_PS, ["plan_id"])
    if not _index_exists(_TABLE_PS, _INDEX_PS_SCENARIO):
        op.create_index(_INDEX_PS_SCENARIO, _TABLE_PS, ["scenario_id"])


def downgrade() -> None:
    if _index_exists(_TABLE_PS, _INDEX_PS_SCENARIO):
        op.drop_index(_INDEX_PS_SCENARIO, table_name=_TABLE_PS)
    if _index_exists(_TABLE_PS, _INDEX_PS_PLAN):
        op.drop_index(_INDEX_PS_PLAN, table_name=_TABLE_PS)
    if _table_exists(_TABLE_PS):
        op.drop_table(_TABLE_PS)
    if _index_exists(_TABLE_PLAN, _INDEX_PLAN_PROJECT):
        op.drop_index(_INDEX_PLAN_PROJECT, table_name=_TABLE_PLAN)
    if _table_exists(_TABLE_PLAN):
        op.drop_table(_TABLE_PLAN)
