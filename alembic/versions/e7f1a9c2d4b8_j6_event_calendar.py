"""J-6(investment_decision_gap_2026-08-29.md):event_calendar。

次回決算日・検証日などの「これから起きるイベント」を、`scores` / `raw_snapshots`
から物理的に分離した専用テーブルに保存する。スコアリング・バックテストからは
参照しない(27.16 のポイントインタイム汚染の再発防止)。

Revision ID: e7f1a9c2d4b8
Revises: c4d9e2b6f8a1
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f1a9c2d4b8"
down_revision: Union[str, Sequence[str], None] = "c4d9e2b6f8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_calendar",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("is_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("collected_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker_id", "event_type", "event_date", name="uq_event_calendar"),
    )
    op.create_index("ix_event_calendar_date", "event_calendar", ["event_date"])
    op.alter_column("event_calendar", "is_estimated", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_event_calendar_date", table_name="event_calendar")
    op.drop_table("event_calendar")
