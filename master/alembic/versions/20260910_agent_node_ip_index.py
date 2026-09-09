"""add index on agent_node.ip

Revision ID: 20260910a1
Revises: 20260908a1
Create Date: 2026-09-10

"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260910a1"
down_revision: Union[str, None] = "20260908a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Agent 启动时按宿主机 IP 查固定 agent_id，加索引加速
    op.create_index("ix_agent_node_ip", "agent_node", ["ip"])


def downgrade() -> None:
    op.drop_index("ix_agent_node_ip", table_name="agent_node")
