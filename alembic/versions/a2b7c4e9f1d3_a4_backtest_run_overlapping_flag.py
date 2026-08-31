"""A-4(docs/defect_and_edge_audit_2026-08-28.md D-2):backtest_runs.overlapping。

評価日の間隔がホライズン未満で保有期間が重なっている実行かどうかを記録する。
非重複(overlapping=False)の実行が「正直な検出力」であり、
`run-backtest --non-overlapping` で併走させる。既存行は overlapping=True で埋める
(いずれも interval 91日 / horizon 365日 の重複実行だった)。

Revision ID: a2b7c4e9f1d3
Revises: f1a2b3c4d5e6
Create Date: 2026-08-28 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a2b7c4e9f1d3"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column("overlapping", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("backtest_runs", "overlapping", server_default=None)


def downgrade() -> None:
    op.drop_column("backtest_runs", "overlapping")
