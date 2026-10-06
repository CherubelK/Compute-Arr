"""job placement fields

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("price_per_hr", sa.Numeric(10, 4), nullable=True))
    op.add_column("jobs", sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "placed_at")
    op.drop_column("jobs", "price_per_hr")
