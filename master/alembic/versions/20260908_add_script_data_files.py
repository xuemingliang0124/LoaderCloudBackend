"""add jmeter_script.data_files column

Revision ID: 20260908a1
Revises:
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260908a1"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jmeter_script",
        sa.Column("data_files", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jmeter_script", "data_files")
