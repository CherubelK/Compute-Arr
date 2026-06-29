"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-06-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("healthy", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.String(64), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("gpu_class", sa.String(64), nullable=False),
        sa.Column("price_per_hr", sa.Numeric(10, 4), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("region", sa.String(128), nullable=True),
        sa.Column("raw_offer_id", sa.String(256), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_price_snapshots_provider_id", "price_snapshots", ["provider_id"])
    op.create_index("ix_price_snapshots_gpu_class", "price_snapshots", ["gpu_class"])
    op.create_index("ix_price_snapshots_ts", "price_snapshots", ["ts"])

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("gpu_class", sa.String(64), nullable=False),
        sa.Column("preference", sa.String(32), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("env_vars", sa.JSON(), nullable=True),
        sa.Column("chosen_provider", sa.String(64), sa.ForeignKey("providers.id"), nullable=True),
        sa.Column("baseline_provider", sa.String(64), nullable=False),
        sa.Column("provider_job_id", sa.String(256), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("actual_cost", sa.Numeric(10, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("ranked_offers", sa.JSON(), nullable=False),
        sa.Column("chosen_provider", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_routing_decisions_job_id", "routing_decisions", ["job_id"])

    op.create_table(
        "failover_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("from_provider", sa.String(64), nullable=False),
        sa.Column("to_provider", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_failover_events_job_id", "failover_events", ["job_id"])


def downgrade() -> None:
    op.drop_table("failover_events")
    op.drop_table("routing_decisions")
    op.drop_table("jobs")
    op.drop_table("price_snapshots")
    op.drop_table("providers")
