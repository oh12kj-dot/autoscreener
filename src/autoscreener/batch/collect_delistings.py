"""上場廃止ユニバースの構築バッチ(docs/defect_and_edge_audit_2026-08-28.md D-1 / I-2)。

`collectors.delisting_source` の薄いオーケストレーション。EDGAR フルインデックスを
走査 → 上場廃止イベントを CIK でティッカーへ解決 → `tickers.delisted_at` に登録。

**段階2(バックテスト母集団への投入)は I-1(XBRL ポイントインタイム)と一体で
実装する。** 廃止銘柄には raw_snapshot が無いため、`build_moic_inputs` の入力を
XBRL companyfacts から作る経路(`scoring.point_in_time_xbrl`)が要る。
"""

from __future__ import annotations

import datetime
import logging

from autoscreener.db.models import DelistingEvent, Ticker
from autoscreener.db.session import session_scope

from autoscreener.collectors.delisting_source import (
    collect_delisting_events,
    register_delisting_events,
)

logger = logging.getLogger(__name__)

# 価格履歴の開始(現状 2023-08)より1年前を既定の走査開始点にする(D-1 推奨)。
DEFAULT_SCAN_START = datetime.date(2022, 8, 1)


def collect_delistings(
    start: datetime.date | None = None, end: datetime.date | None = None
) -> dict[str, int]:
    events = collect_delisting_events(start or DEFAULT_SCAN_START, end)
    logger.info("collected %d delisting filings", len(events))
    counts = register_delisting_events(events)
    counts["events"] = len(events)
    return counts


def backfill_delisting_events_from_tickers(*, observed_at: datetime.datetime | None = None) -> dict[str, int]:
    """Create conservative unknown-classification events from existing master data.

    `Ticker.delisted_at` proves only that listing ceased.  It is intentionally
    not used to infer acquisition, bankruptcy, or settlement economics.
    """
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"eligible": 0, "inserted": 0, "existing": 0}
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.delisted_at.isnot(None)).all()
        counts["eligible"] = len(tickers)
        for ticker in tickers:
            event_date = ticker.delisted_at.date()
            existing = session.query(DelistingEvent.id).filter_by(ticker_id=ticker.id, event_date=event_date, event_type="unknown").first()
            if existing:
                counts["existing"] += 1
                continue
            session.add(DelistingEvent(ticker_id=ticker.id, event_date=event_date, event_type="unknown", source="ticker_master_backfill",
                source_url=None, observed_at=observed_at, confidence="low"))
            counts["inserted"] += 1
    return counts
