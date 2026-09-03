"""Add Model v5 Phase 7 forward-validation table (model_v5_forward_returns).

Revision ID: 2c4e6f8a1b3d
Revises: 1d2e3f4a5b6c
"""

from alembic import op
import sqlalchemy as sa


revision = "2c4e6f8a1b3d"
down_revision = "1d2e3f4a5b6c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_v5_forward_returns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("base_date", sa.Date(), nullable=False),
        sa.Column("horizon", sa.String(length=10), nullable=False),
        sa.Column("realized_return", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("settlement", sa.String(length=10), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticker_id"], ["tickers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "ticker_id", "horizon", name="uq_model_v5_forward_return"),
    )
    op.create_index(
        "ix_model_v5_forward_returns_run_id", "model_v5_forward_returns", ["run_id"], unique=False,
    )
    op.create_index(
        "ix_model_v5_forward_returns_ticker_id", "model_v5_forward_returns", ["ticker_id"], unique=False,
    )
    op.create_index(
        "ix_model_v5_forward_returns_base_date", "model_v5_forward_returns", ["base_date"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_model_v5_forward_returns_base_date", table_name="model_v5_forward_returns")
    op.drop_index("ix_model_v5_forward_returns_ticker_id", table_name="model_v5_forward_returns")
    op.drop_index("ix_model_v5_forward_returns_run_id", table_name="model_v5_forward_returns")
    op.drop_table("model_v5_forward_returns")
