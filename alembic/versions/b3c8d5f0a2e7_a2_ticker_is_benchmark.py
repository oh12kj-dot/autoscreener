"""A-2(defect_and_edge_audit_2026-08-28.md D-4):tickers.is_benchmark。

IWM / IWC / IJR / SPY を「ベンチマーク」として登録できるようにする。価格は
収集・バックフィルするが、除外ゲート・スコアリング・ランキングには一切混ぜない
(`apply_gates` が `included=False, reason='benchmark'` で明示的に外す)。
ポートフォリオ・シミュレーション(D-4)の超過CAGR算出に使う。

Revision ID: b3c8d5f0a2e7
Revises: a2b7c4e9f1d3
Create Date: 2026-08-28 10:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3c8d5f0a2e7"
down_revision: Union[str, Sequence[str], None] = "a2b7c4e9f1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickers",
        sa.Column("is_benchmark", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("tickers", "is_benchmark", server_default=None)


def downgrade() -> None:
    op.drop_column("tickers", "is_benchmark")
