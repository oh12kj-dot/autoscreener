"""Add append-only Model v5 shadow run and score tables.

Revision ID: f0a1b2c3d4e5
Revises: e9b1c3d5f7a9
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f0a1b2c3d4e5"
down_revision = "e9b1c3d5f7a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.String(length=20), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("population_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("mode IN ('shadow', 'active', 'legacy')", name="ck_model_runs_mode"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_model_runs_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_runs_as_of", "model_runs", ["as_of"], unique=False)
    op.create_index("ix_model_runs_status", "model_runs", ["status"], unique=False)
    op.create_index("ix_model_runs_version_as_of", "model_runs", ["model_version", "as_of"], unique=False)

    op.create_table(
        "model_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticker_id", sa.Integer(), nullable=False),
        sa.Column("target_horizon_years", sa.Integer(), nullable=False),
        sa.Column("target_moic", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("distribution", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("states", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticker_id"], ["tickers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "ticker_id", "target_horizon_years", "target_moic",
            name="uq_model_scores_run_ticker_target",
        ),
    )
    op.create_index("ix_model_scores_run_id", "model_scores", ["run_id"], unique=False)
    op.create_index("ix_model_scores_ticker_id", "model_scores", ["ticker_id"], unique=False)
    op.create_index("ix_model_scores_run_confidence", "model_scores", ["run_id", "confidence"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_scores_run_confidence", table_name="model_scores")
    op.drop_index("ix_model_scores_ticker_id", table_name="model_scores")
    op.drop_index("ix_model_scores_run_id", table_name="model_scores")
    op.drop_table("model_scores")
    op.drop_index("ix_model_runs_version_as_of", table_name="model_runs")
    op.drop_index("ix_model_runs_status", table_name="model_runs")
    op.drop_index("ix_model_runs_as_of", table_name="model_runs")
    op.drop_table("model_runs")
