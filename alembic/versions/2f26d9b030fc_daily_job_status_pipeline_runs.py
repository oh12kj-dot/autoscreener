"""daily job status: pipeline_runs and pipeline_stage_runs
(daily_job_status_screen_2026-08-30.md、14.15の運用監視)。

2026-08-29の実運用で全銘柄隔離・収集0件・スコアリング中断・提出書類収集の
例外落ちが同時発生したにもかかわらず、パイプラインの終了コードは0だった
(§0)。「終了コード0」と「正常」を同一視しないための実行記録テーブル。

`pipeline_runs` はパイプライン1回分の実行単位、`pipeline_stage_runs` は
その中の工程1つ分。`collection_logs`(銘柄単位のログ)とは粒度が異なるため
別テーブルにする(run_id は無関係な独立したuuid)。

Revision ID: 2f26d9b030fc
Revises: f9b2d6e1a3c7
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "2f26d9b030fc"
down_revision: Union[str, Sequence[str], None] = "f9b2d6e1a3c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("is_weekly", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("health", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.alter_column("pipeline_runs", "is_weekly", server_default=None)
    op.create_index("ix_pipeline_runs_run_id", "pipeline_runs", ["run_id"], unique=True)
    op.create_index("ix_pipeline_runs_run_date", "pipeline_runs", ["run_date"])
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])

    op.create_table(
        "pipeline_stage_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_traceback", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "stage", name="uq_stage_run_stage"),
    )
    op.create_index("ix_pipeline_stage_runs_run_id", "pipeline_stage_runs", ["run_id"])
    op.create_index("ix_pipeline_stage_runs_run_seq", "pipeline_stage_runs", ["run_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_stage_runs_run_seq", table_name="pipeline_stage_runs")
    op.drop_index("ix_pipeline_stage_runs_run_id", table_name="pipeline_stage_runs")
    op.drop_table("pipeline_stage_runs")

    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_run_date", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_run_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
