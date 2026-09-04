"""incremental daily collection metadata

Revision ID: a6d8e0f2b4c6
Revises: 91c1fa3f0534
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6d8e0f2b4c6"
down_revision: str | None = "91c1fa3f0534"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("price_snapshots", sa.Column("shares_observed_at", sa.Date(), nullable=True))
    op.add_column("price_snapshots", sa.Column("shares_coverage_status", sa.String(40), nullable=True))
    op.execute(
        "UPDATE price_snapshots SET shares_observed_at = trade_date, "
        "shares_coverage_status = 'legacy_observation' WHERE shares_outstanding IS NOT NULL"
    )

    op.create_table(
        "collection_cursors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("scope", sa.String(80), nullable=False),
        sa.Column("cursor_date", sa.Date(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source", "scope", name="uq_collection_cursor_source_scope"),
    )
    op.create_table(
        "source_processing_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False),
        sa.Column("processor", sa.String(60), nullable=False),
        sa.Column("processor_version", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "source_type", "source_key", "processor", "processor_version",
            name="uq_source_processing_ledger_key",
        ),
    )
    op.create_index("ix_source_processing_ledger_ticker_id", "source_processing_ledger", ["ticker_id"])
    op.create_index(
        "ix_source_processing_ledger_processor", "source_processing_ledger",
        ["processor", "processor_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_processing_ledger_processor", table_name="source_processing_ledger")
    op.drop_index("ix_source_processing_ledger_ticker_id", table_name="source_processing_ledger")
    op.drop_table("source_processing_ledger")
    op.drop_table("collection_cursors")
    op.drop_column("price_snapshots", "shares_coverage_status")
    op.drop_column("price_snapshots", "shares_observed_at")
