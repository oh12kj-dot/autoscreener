"""NYSE session-aware gating for market-data stages.

The scheduled job runs at 09:00 JST, so a local-weekday test is wrong: Saturday
morning in Japan follows Friday's U.S. close and must collect it.  Decisions are
therefore based on the latest *completed* XNYS session and broad DB coverage.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from dataclasses import dataclass

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.db.models import PriceSnapshot, Ticker


@dataclass(frozen=True)
class MarketSessionDecision:
    should_run: bool
    expected_session: datetime.date
    latest_covered_session: datetime.date | None
    covered_count: int
    target_count: int
    symbols_to_collect: tuple[str, ...] = ()
    reason: str | None = None


def latest_completed_us_session(now: datetime.datetime | None = None) -> datetime.date:
    """Return the latest NYSE core session whose close is not later than ``now``."""
    current = now or datetime.datetime.now(datetime.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.UTC)
    current = current.astimezone(datetime.UTC)
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(
        pd.Timestamp((current - datetime.timedelta(days=14)).date()),
        pd.Timestamp((current + datetime.timedelta(days=1)).date()),
    )
    completed = [
        session
        for session in sessions
        if calendar.session_close(session).to_pydatetime() <= current
    ]
    if not completed:  # pragma: no cover - the 14-day window always contains sessions
        raise RuntimeError("no completed XNYS session found in lookback window")
    return completed[-1].date()


def assess_market_session(
    session: Session,
    *,
    target_count: int,
    minimum_coverage: float,
    symbols: Sequence[str] | None = None,
    now: datetime.datetime | None = None,
) -> MarketSessionDecision:
    """Decide whether another full market-data pass can add a completed session.

    A single erroneous future-dated row must not suppress collection for the whole
    universe.  A date counts as completed locally only when at least the configured
    fraction of the current collection population has a price row for that date.
    """
    expected = latest_completed_us_session(now)
    normalized_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in (symbols or ())))
    if target_count == 0:
        return MarketSessionDecision(
            should_run=False,
            expected_session=expected,
            latest_covered_session=None,
            covered_count=0,
            target_count=0,
            symbols_to_collect=(),
            reason="no_collection_targets",
        )
    required = max(1, math.ceil(target_count * minimum_coverage))
    covered_symbols: set[str] = set()
    if normalized_symbols:
        covered_symbols = {
            row[0]
            for row in session.query(Ticker.symbol)
            .join(PriceSnapshot, PriceSnapshot.ticker_id == Ticker.id)
            .filter(
                Ticker.delisted_at.is_(None),
                Ticker.symbol.in_(normalized_symbols),
                PriceSnapshot.trade_date == expected,
            )
            .distinct()
            .all()
        }
    symbols_to_collect = tuple(
        symbol for symbol in normalized_symbols if symbol not in covered_symbols
    )
    coverage_query = (
        session.query(PriceSnapshot.trade_date, func.count(func.distinct(PriceSnapshot.ticker_id)))
        .join(Ticker, Ticker.id == PriceSnapshot.ticker_id)
        .filter(Ticker.delisted_at.is_(None))
    )
    if symbols is not None:
        coverage_query = coverage_query.filter(Ticker.symbol.in_([symbol.upper() for symbol in symbols]))
    covered_rows = (
        coverage_query
        .group_by(PriceSnapshot.trade_date)
        .having(func.count(func.distinct(PriceSnapshot.ticker_id)) >= required)
        .order_by(PriceSnapshot.trade_date.desc())
        .first()
    )
    latest = covered_rows[0] if covered_rows is not None else None
    covered_count = int(covered_rows[1]) if covered_rows is not None else 0
    # Even when the session crosses the broad-coverage threshold, collect only
    # the missing tail instead of accepting a permanent gap or refetching all.
    should_run = bool(symbols_to_collect) if normalized_symbols else latest is None or latest < expected
    return MarketSessionDecision(
        should_run=should_run,
        expected_session=expected,
        latest_covered_session=latest,
        covered_count=len(covered_symbols) if normalized_symbols else covered_count,
        target_count=target_count,
        symbols_to_collect=symbols_to_collect,
        reason=(
            None
            if should_run
            else "no_new_market_session"
        ),
    )
