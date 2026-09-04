"""State checks for the weekly yfinance statement-refresh work item.

The price-session decision is deliberately not reused here.  A complete price
session says nothing about whether the five financial statements were fetched.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from autoscreener.collectors.snapshot_collector import _STATEMENTS_AS_OF_KEY
from autoscreener.db.models import CollectionCursor, RawSnapshot, Ticker

CURSOR_SOURCE = "yfinance"
CURSOR_SCOPE = "weekly_statement_refresh"


def market_week_start(session_date: date) -> date:
    """Return Monday for the completed US-market week containing ``session_date``."""
    return session_date - timedelta(days=session_date.weekday())


def active_symbols_missing_statement_observation(
    session: Session,
    week_start: date,
    *,
    eligible_symbols: set[str] | None = None,
) -> list[str]:
    """Return active symbols without an observed statement fetch for this week.

    A legacy payload without the marker remains unknown.  It is never assigned
    a guessed observation date; the refresh subsequently records a real one.
    """
    if eligible_symbols is not None and not eligible_symbols:
        return []

    # One windowed query replaces the previous ticker-by-ticker lookup.  At
    # production size that implementation issued about 5,800 SELECTs before a
    # single provider request and defeated the point of the incremental job.
    ranked_raw = select(
        RawSnapshot.ticker_id.label("ticker_id"),
        RawSnapshot.payload.label("payload"),
        func.row_number().over(
            partition_by=RawSnapshot.ticker_id,
            order_by=(RawSnapshot.snapshot_date.desc(), RawSnapshot.id.desc()),
        ).label("row_number"),
    ).subquery()
    query = (
        session.query(Ticker.symbol, ranked_raw.c.payload)
        .outerjoin(
            ranked_raw,
            and_(ranked_raw.c.ticker_id == Ticker.id, ranked_raw.c.row_number == 1),
        )
        .filter(Ticker.delisted_at.is_(None), Ticker.is_benchmark.is_(False))
        .order_by(Ticker.symbol)
    )
    if eligible_symbols is not None:
        query = query.filter(Ticker.symbol.in_(sorted(eligible_symbols)))

    missing: list[str] = []
    for symbol, payload in query.all():
        observed = (payload or {}).get(_STATEMENTS_AS_OF_KEY)
        try:
            is_current = date.fromisoformat(observed) >= week_start
        except (TypeError, ValueError):
            is_current = False
        if not is_current:
            missing.append(symbol)
    return missing


def refresh_is_due(session: Session, week_start: date) -> bool:
    cursor = (
        session.query(CollectionCursor)
        .filter_by(source=CURSOR_SOURCE, scope=CURSOR_SCOPE)
        .one_or_none()
    )
    return cursor is None or cursor.cursor_date < week_start


def mark_refresh_complete(session: Session, week_start: date, *, scheduled_count: int) -> None:
    cursor = (
        session.query(CollectionCursor)
        .filter_by(source=CURSOR_SOURCE, scope=CURSOR_SCOPE)
        .one_or_none()
    )
    detail = {
        "scheduled_before_refresh": scheduled_count,
        "observation_policy": "only provider fetches set _statements_as_of; legacy dates are not inferred",
    }
    if cursor is None:
        session.add(CollectionCursor(source=CURSOR_SOURCE, scope=CURSOR_SCOPE, cursor_date=week_start, detail=detail))
    else:
        cursor.cursor_date = week_start
        cursor.detail = detail
