"""model feature values (WP-D D-4)

Revision ID: c80f29dab3b6
Revises: a6d8e0f2b4c6
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c80f29dab3b6"
down_revision: str | None = "a6d8e0f2b4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_feature_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(),
            sa.ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "ticker_id", sa.Integer(),
            sa.ForeignKey("tickers.id"), nullable=False,
        ),
        sa.Column("feature_key", sa.String(64), nullable=False),
        sa.Column("value", sa.Numeric(24, 12), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("coverage_status", sa.String(30), nullable=False),
        sa.Column("status", sa.String(100), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reliability", sa.Numeric(6, 5), nullable=True),
        sa.Column("missing_reason", sa.String(100), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "run_id", "ticker_id", "feature_key",
            name="uq_model_feature_values_run_ticker_feature",
        ),
    )
    op.create_index("ix_model_feature_values_run_id", "model_feature_values", ["run_id"])
    op.create_index("ix_model_feature_values_ticker_id", "model_feature_values", ["ticker_id"])
    op.create_index(
        "ix_model_feature_values_run_feature", "model_feature_values", ["run_id", "feature_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_feature_values_run_feature", table_name="model_feature_values")
    op.drop_index("ix_model_feature_values_ticker_id", table_name="model_feature_values")
    op.drop_index("ix_model_feature_values_run_id", table_name="model_feature_values")
    op.drop_table("model_feature_values")
