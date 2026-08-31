"""B-6(docs/defect_and_edge_audit_2026-08-28.md D-11):price_snapshots.dividend。

その取引日(ex-date)の1株あたり配当。`backtest.runner._realized_return` と
`forward_validation` の実現リターンを価格リターンから総リターンへ変えるのに使う。
既存行は NULL(=未収集)。バックフィルを配当込みで再実行すると埋まる。

Revision ID: c4d9e2b6f8a1
Revises: b3c8d5f0a2e7
Create Date: 2026-08-28 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4d9e2b6f8a1"
down_revision: Union[str, Sequence[str], None] = "b3c8d5f0a2e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("price_snapshots", sa.Column("dividend", sa.Numeric(18, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("price_snapshots", "dividend")
