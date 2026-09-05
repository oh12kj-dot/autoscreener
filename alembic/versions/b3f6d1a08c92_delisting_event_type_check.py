"""delisting_events.event_type CHECK constraint (delisting label backfill 2026-09-04)

`event_type` は元々 `String(30)` で自由記述だった。94件の実データは全件
`unknown` だが、`docs/delisting_label_backfill_2026-09-04.md` の調査で
"unknown を推測で埋めない" 方針(監査 §5.3)を型レベルでも守るため、許容値を
CHECK制約で固定する。値は**新規に作らず**、既に `backtest/runner.py:472-489`
(実現リターン計算)と `api/routes.py` のM&A履歴エンドポイントが消費している
6値——`unknown` / `cash_acquisition` / `stock_acquisition` / `bankruptcy` /
`liquidation` / `exchange_transfer`——とそのまま一致させる(独自の値を追加
すると、その2箇所が知らない値を `unknown` 相当として静かに読み違える)。
`unknown` は必ず含む——分類できない事象を落とさないための値であり、default
にもしない(既存のデフォルト無し・NOT NULLのまま)。

`autoscreener.collectors.delisting_classification.EVENT_TYPES` と同期させること。

Revision ID: b3f6d1a08c92
Revises: c80f29dab3b6
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b3f6d1a08c92"
down_revision: str | None = "c80f29dab3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# autoscreener.collectors.delisting_classification.EVENT_TYPES と同期させること。
_EVENT_TYPES = (
    "unknown",
    "cash_acquisition",
    "stock_acquisition",
    "bankruptcy",
    "liquidation",
    "exchange_transfer",
)
_CONSTRAINT_NAME = "ck_delisting_events_event_type"


def upgrade() -> None:
    values = ", ".join(f"'{v}'" for v in _EVENT_TYPES)
    op.execute(
        f"ALTER TABLE delisting_events ADD CONSTRAINT {_CONSTRAINT_NAME} "
        f"CHECK (event_type IN ({values}))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE delisting_events DROP CONSTRAINT {_CONSTRAINT_NAME}")
