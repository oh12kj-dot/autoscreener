"""State checks for the weekly yfinance statement-refresh work item.

The price-session decision is deliberately not reused here.  A complete price
session says nothing about whether the five financial statements were fetched.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from autoscreener.collectors.snapshot_collector import _STATEMENTS_AS_OF_KEY
from autoscreener.db.models import CollectionCursor, RawSnapshot, Ticker

CURSOR_SOURCE = "yfinance"
CURSOR_SCOPE = "weekly_statement_refresh"


def market_week_start(session_date: date) -> date:
    """Return Monday for the completed US-market week containing ``session_date``."""
    return session_date - timedelta(days=session_date.weekday())


def active_symbols_missing_statement_observation(session: Session, week_start: date) -> list[str]:
    """Return active symbols without an observed statement fetch for this week.

    A legacy payload without the marker remains unknown.  It is never assigned
    a guessed observation date; the refresh subsequently records a real one.
    """
    missing: list[str] = []
    for ticker in session.query(Ticker).filter(Ticker.delisted_at.is_(None)).order_by(Ticker.symbol):
        raw = (
            session.query(RawSnapshot.payload)
            .filter(RawSnapshot.ticker_id == ticker.id)
            .order_by(RawSnapshot.snapshot_date.desc())
            .first()
        )
        observed = raw[0].get(_STATEMENTS_AS_OF_KEY) if raw is not None else None
        try:
            is_current = date.fromisoformat(observed) >= week_start
        except (TypeError, ValueError):
            is_current = False
        if not is_current:
            missing.append(ticker.symbol)
    return missing


def refresh_is_due(session: Session, week_start: date) -> bool:
    cursor = (
        session.query(CollectionCursor)
        .filter_by(source=CURSOR_SOURCE, scope=CURSOR_SCOPE)
        .one_or_none()
    )
    return cursor is None or cursor.cursor_date < week_start


def mark_refresh_complete(session: Session, week_start: date, *, legacy_unmarked: int) -> None:
    cursor = (
        session.query(CollectionCursor)
        .filter_by(source=CURSOR_SOURCE, scope=CURSOR_SCOPE)
        .one_or_none()
    )
    detail = {
        "legacy_unmarked_before_refresh": legacy_unmarked,
        "observation_policy": "only provider fetches set _statements_as_of; legacy dates are not inferred",
    }
    if cursor is None:
        session.add(CollectionCursor(source=CURSOR_SOURCE, scope=CURSOR_SCOPE, cursor_date=week_start, detail=detail))
    else:
        cursor.cursor_date = week_start
        cursor.detail = detail
