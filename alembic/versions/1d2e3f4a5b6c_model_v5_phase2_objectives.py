"""Add re-rankable Model v5 objective scores.

Revision ID: 1d2e3f4a5b6c
Revises: f0a1b2c3d4e5
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "1d2e3f4a5b6c"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "objective_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("objective", sa.String(length=50), nullable=False),
        sa.Column("score_value", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("explanation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticker_id"], ["tickers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "ticker_id", "objective",
            name="uq_objective_scores_run_ticker_objective",
        ),
    )
    op.create_index("ix_objective_scores_run_id", "objective_scores", ["run_id"], unique=False)
    op.create_index("ix_objective_scores_ticker_id", "objective_scores", ["ticker_id"], unique=False)
    op.create_index(
        "ix_objective_scores_run_objective_rank",
        "objective_scores", ["run_id", "objective", "rank"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_objective_scores_run_objective_rank", table_name="objective_scores")
    op.drop_index("ix_objective_scores_ticker_id", table_name="objective_scores")
    op.drop_index("ix_objective_scores_run_id", table_name="objective_scores")
    op.drop_table("objective_scores")

