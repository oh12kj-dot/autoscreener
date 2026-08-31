"""A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):スコア行にデータ鮮度を残す。

`scores.price_as_of` / `scores.financials_as_of` を追加する。日次バッチが一斉
隔離(D-12)などで途中停止しても `run_scoring` は前日以前のデータで当日付の
ランキングを書き続けられてしまい、その事実がどこにも出ていなかった。

- `price_as_of`      … そのスコアの時価総額・モメンタムに使った終値の取引日
- `financials_as_of` … そのスコアが読んだ raw_snapshot の available_from

APIは `score_date` との営業日差を `data_age_days` として返し、UIが古い
ランキングを明示できるようにする。既存行は NULL(=不明)のままにする——
False相当の値を埋めると「鮮度は確認済み」と誤読される。

Revision ID: f1a2b3c4d5e6
Revises: 3c09c1812341
Create Date: 2026-08-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "3c09c1812341"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scores", sa.Column("price_as_of", sa.Date(), nullable=True))
    op.add_column("scores", sa.Column("financials_as_of", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("scores", "financials_as_of")
    op.drop_column("scores", "price_as_of")
