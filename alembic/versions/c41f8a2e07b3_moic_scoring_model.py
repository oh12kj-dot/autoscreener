"""moic scoring model: replace subscore columns, add delisting settlement, add backtest_runs

要件定義書27章。旧v2(8サブスコアのパーセンタイル加重幾何平均)から
実現時価総額倍率(implied MOIC)モデルへの移行に伴うスキーマ変更。

**既存のスコア行は削除する。** `subscores` 等を落とした時点で旧行は解釈不能に
なり、`probability` が NULL のまま残ってランキングとKPI集計を汚染するため。
スコアは日次バッチが再生成できる派生データであり、原本(`raw_snapshots` /
`price_snapshots`)は一切触らない。

Revision ID: c41f8a2e07b3
Revises: 9c31a7f0b4d2
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c41f8a2e07b3"
down_revision: str | Sequence[str] | None = "9c31a7f0b4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧モデルのスコアは新スキーマでは解釈できない(上記docstring)。
    op.execute("DELETE FROM scores")

    op.drop_column("scores", "overall_score")
    op.drop_column("scores", "subscores")
    op.drop_column("scores", "subscore_fallback")
    op.drop_column("scores", "subscore_metric_coverage")
    op.drop_column("scores", "coverage_ratio")

    op.add_column("scores", sa.Column("probability", sa.Numeric(10, 8), nullable=True))
    op.add_column("scores", sa.Column("median_moic", sa.Numeric(12, 4), nullable=True))
    op.add_column("scores", sa.Column("log_moic_mu", sa.Numeric(10, 5), nullable=True))
    op.add_column("scores", sa.Column("log_moic_sigma", sa.Numeric(10, 5), nullable=True))
    op.add_column("scores", sa.Column("survival_probability", sa.Numeric(6, 5), nullable=True))
    op.add_column("scores", sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(
        "ix_scores_date_version_probability", "scores", ["score_date", "scoring_version", "probability"]
    )

    # 27.11:上場廃止でどう決済されたかを残す。既存行は市場価格で評価済み。
    op.add_column("forward_returns", sa.Column("settlement", sa.String(10), nullable=True))
    op.execute("UPDATE forward_returns SET settlement = 'market' WHERE realized_return IS NOT NULL")

    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scoring_version", sa.String(20), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("rebalance_dates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_runs_config_hash", "backtest_runs", ["config_hash"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_config_hash", table_name="backtest_runs")
    op.drop_table("backtest_runs")

    op.drop_column("forward_returns", "settlement")

    op.drop_index("ix_scores_date_version_probability", table_name="scores")
    op.drop_column("scores", "factors")
    op.drop_column("scores", "survival_probability")
    op.drop_column("scores", "log_moic_sigma")
    op.drop_column("scores", "log_moic_mu")
    op.drop_column("scores", "median_moic")
    op.drop_column("scores", "probability")

    op.add_column("scores", sa.Column("coverage_ratio", sa.Numeric(4, 3), nullable=True))
    op.add_column(
        "scores", sa.Column("subscore_metric_coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("scores", sa.Column("subscore_fallback", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("scores", sa.Column("subscores", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("scores", sa.Column("overall_score", sa.Numeric(6, 2), nullable=True))
