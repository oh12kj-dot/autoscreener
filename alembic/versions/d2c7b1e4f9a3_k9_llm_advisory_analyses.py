"""K-9:LLM(Claude API)の定性分析の保存先。

**原則3(a1b2c3d4e5f6 のK-1テーブル群と同じ):この表はゲート
(`evaluate_gates`)にもスコア(`scoring/`)にも入れない。** 表示・投資ノート
起草・人間の下読みのみ。LLMの出力は同じ入力でも揺れるため、除外や順位づけの
根拠にした瞬間、バックテスト(14.3)が再現できなくなる。表を分けているのは、
その約束を人間の記憶ではなくスキーマで守るため。

**一意性を部分インデックス2本で担保している理由**:`daily_report` は銘柄
横断なので `ticker_id` が NULL になる。Postgres の UNIQUE 制約は NULL 同士を
「異なる」と扱うため、単一の UNIQUE では同じ日のレポートを何度でも
書き込めてしまう。ダミー銘柄を作って回避するのは、存在しない銘柄をDBに
足すことになるので採らない。

Revision ID: d2c7b1e4f9a3
Revises: a1b2c3d4e5f6
Create Date: 2026-08-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2c7b1e4f9a3"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        # 銘柄横断の出力(daily_report)では NULL。
        sa.Column("ticker_id", sa.Integer(), sa.ForeignKey("tickers.id"), nullable=True, index=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=60), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("effort", sa.String(length=10), nullable=False),
        sa.Column("prompt_fingerprint", sa.String(length=64), nullable=False),
        # テキスト出力(Markdown)。構造化系では NULL。
        sa.Column("content", sa.Text(), nullable=True),
        # 構造化出力。テキスト系では NULL。
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "uq_llm_analyses_ticker",
        "llm_analyses",
        ["ticker_id", "kind", "source_key", "prompt_fingerprint"],
        unique=True,
        postgresql_where=sa.text("ticker_id IS NOT NULL"),
    )
    op.create_index(
        "uq_llm_analyses_global",
        "llm_analyses",
        ["kind", "source_key", "prompt_fingerprint"],
        unique=True,
        postgresql_where=sa.text("ticker_id IS NULL"),
    )
    op.create_index("ix_llm_analyses_kind_as_of", "llm_analyses", ["kind", "as_of"])


def downgrade() -> None:
    op.drop_index("ix_llm_analyses_kind_as_of", table_name="llm_analyses")
    op.drop_index("uq_llm_analyses_global", table_name="llm_analyses")
    op.drop_index("uq_llm_analyses_ticker", table_name="llm_analyses")
    op.drop_table("llm_analyses")
