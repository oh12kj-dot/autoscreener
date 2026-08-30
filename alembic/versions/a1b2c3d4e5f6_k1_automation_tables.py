"""K-1(自動化計画 2026-08-30):判断材料の自動抽出テーブル群。

投資ノートで人間が手入力していた項目を機械で埋めるための保存先をまとめて置く。
**5テーブルを1マイグレーションにまとめている**のは、30.11「マイグレーションを
並行着手しない」を守りつつ K-2〜K-5 の実装を並行させるため——器を先に1度で
確定させ、以降の作業はコードだけに閉じる。

- `filing_sections` … 10-K/10-Q/8-K の本文を Item 単位で切り出した原文。
  以降の抽出(顧客集中・訴訟・希薄化条項)はすべてこの表を読む。再取得は
  SECへの再アクセスを伴うので、切り出し結果は必ず保存する。
- `dilution_capacity` … シェルフ残枠・ATM残枠・変動転換条項。
  `research/<TICKER>.md` の `dilution:` ブロックの機械版。
- `customer_concentration` … 10%超顧客の開示。TEMPLATE が指標名
  `customer_concentration_disclosed_drop` を要求しているのに実装が無かった穴。
- `guidance` … 8-K EX-99.1(決算プレスリリース)のガイダンス数値。
  トランスクリプトが有料でも、ガイダンスの原文はEDGARに無料である。
- `litigation_events` … 証券集団訴訟・SEC調査・ショートレポート起因の開示。

**原則3:これらはゲート(`evaluate_gates`)にもスコア(`scoring/`)にも入れない。**
表示・チェックリスト・ノート起草・アラートのみ。

Revision ID: a1b2c3d4e5f6
Revises: 2f26d9b030fc
Create Date: 2026-08-30 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "2f26d9b030fc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "filing_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("accession_number", sa.String(length=25), nullable=False),
        sa.Column("form", sa.String(length=20), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=False),
        sa.Column("section", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("extracted_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("accession_number", "section", name="uq_filing_section"),
    )
    op.create_index("ix_filing_sections_ticker_section", "filing_sections", ["ticker_id", "section"])

    op.create_table(
        "dilution_capacity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("shelf_registered_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("shelf_remaining_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("atm_authorized_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("atm_remaining_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("has_variable_conversion", sa.Boolean(), nullable=True),
        sa.Column("unexercised_options_shares", sa.Numeric(24, 4), nullable=True),
        sa.Column("unexercised_options_ratio", sa.Numeric(10, 6), nullable=True),
        sa.Column("source_form", sa.String(length=20), nullable=True),
        sa.Column("source_accession", sa.String(length=25), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("collected_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker_id", "as_of_date", name="uq_dilution_capacity"),
    )

    op.create_table(
        "customer_concentration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("customer_label", sa.String(length=100), nullable=False),
        sa.Column("revenue_pct", sa.Numeric(10, 6), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),  # 'xbrl' / 'text'
        sa.Column("source_accession", sa.String(length=25), nullable=True),
        sa.Column("collected_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker_id", "period_end", "customer_label", name="uq_customer_concentration"),
    )

    op.create_table(
        "guidance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("filed_date", sa.Date(), nullable=False),
        sa.Column("accession_number", sa.String(length=25), nullable=False),
        sa.Column("period_label", sa.String(length=40), nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),  # 'revenue' / 'gross_margin' / ...
        sa.Column("low_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("high_usd", sa.Numeric(24, 2), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("collected_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("accession_number", "metric", "period_label", name="uq_guidance"),
    )

    op.create_table(
        "litigation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=False, index=True),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),  # 'class_action' / 'sec_investigation' / 'short_report'
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("source_accession", sa.String(length=25), nullable=True),
        sa.Column("collected_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("ticker_id", "kind", "event_date", "title", name="uq_litigation_event"),
    )


def downgrade() -> None:
    op.drop_table("litigation_events")
    op.drop_table("guidance")
    op.drop_table("customer_concentration")
    op.drop_table("dilution_capacity")
    op.drop_index("ix_filing_sections_ticker_section", table_name="filing_sections")
    op.drop_table("filing_sections")
