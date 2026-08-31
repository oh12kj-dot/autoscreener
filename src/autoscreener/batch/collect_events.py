"""カタリスト・カレンダーの収集バッチ(J-6、docs/investment_decision_gap_2026-08-29.md)。

対象は 30.3.4 の**追跡対象銘柄**(保有 + 上位N + ノートあり)に限定する。
全銘柄には広げない(レート制限 8.3・14.9)。yfinance `Ticker.calendar` の
**次回決算日のみ**採用し、過去日は捨てる。

**このモジュールは `scoring/` / `backtest/` から import してはならない**
(27.16 のポイントインタイム汚染の再発防止。テストで固定している)。
`run-daily-pipeline` の週次(月曜)工程に置き、失敗してもパイプラインは止めない。
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from autoscreener.collectors.calendar_source import fetch_calendar, parse_next_earnings_date
from autoscreener.config import load_positions_config
from autoscreener.dates import utc_today
from autoscreener.db.models import EventCalendar, Score, Ticker
from autoscreener.db.session import session_scope
from autoscreener.research.notes import load_all_notes

logger = logging.getLogger(__name__)

_DEFAULT_TICKER_LIMIT = 300


def select_event_tickers(session: Session, *, limit: int) -> list[Ticker]:
    """追跡対象銘柄(30.3.4 の和集合)。CIK は不要なので `select_tracked_tickers` とは別実装。"""
    positions = load_positions_config()
    position_symbols = {p.ticker.upper() for p in positions.positions if p.closed_on is None}
    note_symbols = set(load_all_notes().keys())

    latest_score_date = session.query(Score.score_date).order_by(Score.score_date.desc()).limit(1).scalar()
    ranked_symbols: list[str] = []
    if latest_score_date is not None:
        remaining = max(0, limit - len(position_symbols | note_symbols))
        rows = (
            session.query(Ticker.symbol)
            .join(Score, Score.ticker_id == Ticker.id)
            .filter(Score.score_date == latest_score_date, Score.probability.isnot(None))
            .order_by(Score.probability.desc())
            .limit(remaining)
            .all()
        )
        ranked_symbols = [r[0] for r in rows]

    all_symbols = position_symbols | note_symbols | set(ranked_symbols)
    if not all_symbols:
        return []
    return session.query(Ticker).filter(Ticker.symbol.in_(all_symbols)).limit(limit).all()


def _upsert_event(
    session: Session,
    ticker_id: int,
    event_type: str,
    event_date: datetime.date,
    *,
    source: str,
    collected_on: datetime.date,
    is_estimated: bool,
) -> bool:
    """同一 (ticker_id, event_type, event_date) が無ければ行を作る。戻り値は新規作成か。"""
    existing = (
        session.query(EventCalendar)
        .filter_by(ticker_id=ticker_id, event_type=event_type, event_date=event_date)
        .one_or_none()
    )
    if existing is not None:
        return False
    session.add(
        EventCalendar(
            ticker_id=ticker_id,
            event_type=event_type,
            event_date=event_date,
            is_estimated=is_estimated,
            source=source,
            collected_on=collected_on,
        )
    )
    return True


def collect_events(
    symbols: list[str] | None = None,
    *,
    limit: int = _DEFAULT_TICKER_LIMIT,
    as_of: datetime.date | None = None,
    calendar_fetcher=fetch_calendar,
) -> dict[str, int]:
    """追跡対象銘柄の次回決算日を集める。`calendar_fetcher` はテストで差し替え可能。

    戻り値は {"tickers": n, "new_events": n, "existing": n, "no_date": n}。
    """
    today = as_of or utc_today()
    counts = {"tickers": 0, "new_events": 0, "existing": 0, "no_date": 0}

    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_event_tickers(session, limit=limit)

        for ticker in tickers:
            counts["tickers"] += 1
            try:
                calendar = calendar_fetcher(ticker.symbol)
            except Exception:  # noqa: BLE001
                logger.warning("calendar fetch failed for %s; skipping", ticker.symbol, exc_info=True)
                calendar = None
            next_date = parse_next_earnings_date(calendar, today)
            if next_date is None:
                counts["no_date"] += 1
                continue
            created = _upsert_event(
                session,
                ticker.id,
                "earnings",
                next_date,
                source="yfinance",
                collected_on=today,
                is_estimated=True,
            )
            counts["new_events" if created else "existing"] += 1

    return counts
