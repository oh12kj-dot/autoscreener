"""J-7(investment_decision_gap_2026-08-29.md):insider_transactions / short_interest。

Form 4 由来のインサイダー取引と FINRA の空売り残。**原則3:ゲート・スコアには
一切入れない**——表示とアラートのみ。空売り残は遅延があるので `published_date` /
`settlement_date` を必ず持たせる。

Revision ID: f9b2d6e1a3c7
Revises: e7f1a9c2d4b8
Create Date: 2026-08-29 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f9b2d6e1a3c7"
down_revision: Union[str, Sequence[str], None] = "e7f1a9c2d4b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "insider_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("accession_number", sa.String(length=25), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("insider_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=True),
        sa.Column("transaction_code", sa.String(length=4), nullable=False),
        sa.Column("shares", sa.Numeric(24, 4), nullable=False),
        sa.Column("price_usd", sa.Numeric(20, 4), nullable=True),
        sa.Column("value_usd", sa.Numeric(24, 4), nullable=True),
        sa.Column("is_derivative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "accession_number", "insider_name", "transaction_date", "transaction_code", "shares",
            name="uq_insider_transaction",
        ),
    )
    op.create_index(
        "ix_insider_tx_ticker_date", "insider_transactions", ["ticker_id", "transaction_date"]
    )
    op.alter_column("insider_transactions", "is_derivative", server_default=None)

    op.create_table(
        "short_interest",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column("short_interest_shares", sa.Numeric(24, 4), nullable=False),
        sa.Column("avg_daily_volume", sa.Numeric(24, 4), nullable=True),
        sa.Column("days_to_cover", sa.Numeric(14, 4), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker_id", "settlement_date", name="uq_short_interest"),
    )
    op.create_index("ix_short_interest_settlement", "short_interest", ["settlement_date"])


def downgrade() -> None:
    op.drop_index("ix_short_interest_settlement", table_name="short_interest")
    op.drop_table("short_interest")
    op.drop_index("ix_insider_tx_ticker_date", table_name="insider_transactions")
    op.drop_table("insider_transactions")
