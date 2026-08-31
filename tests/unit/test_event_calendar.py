"""J-6(docs/investment_decision_gap_2026-08-29.md):カタリスト・カレンダーのテスト。"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from autoscreener.batch.collect_events import collect_events
from autoscreener.collectors.calendar_source import parse_next_earnings_date
from autoscreener.db.models import EventCalendar, Ticker
from autoscreener.db.session import session_scope

_TODAY = datetime.date(2026, 8, 29)


# --- 純パーサ ---------------------------------------------------------------


def test_parse_returns_none_for_empty_or_missing_key():
    assert parse_next_earnings_date(None, _TODAY) is None
    assert parse_next_earnings_date({}, _TODAY) is None
    assert parse_next_earnings_date({"Ex-Dividend Date": datetime.date(2026, 9, 1)}, _TODAY) is None


def test_parse_discards_past_dates():
    cal = {"Earnings Date": [datetime.date(2026, 5, 1), datetime.date(2026, 6, 1)]}
    assert parse_next_earnings_date(cal, _TODAY) is None


def test_parse_picks_nearest_future_date():
    cal = {"Earnings Date": [datetime.date(2026, 12, 1), datetime.date(2026, 9, 15)]}
    assert parse_next_earnings_date(cal, _TODAY) == datetime.date(2026, 9, 15)
    # 文字列でも通る
    assert parse_next_earnings_date({"Earnings Date": "2026-10-05"}, _TODAY) == datetime.date(2026, 10, 5)


# --- ポイントインタイム隔離(最重要) ------------------------------------------


def test_scoring_and_backtest_never_import_event_calendar():
    """27.16:次回決算日はモデルから物理的に隔離する。`scoring/` と `backtest/` の
    ソースに `event_calendar` / `EventCalendar` / `collect_events` の文字列が
    現れないことを assert する(再発防止)。"""
    root = Path(__file__).resolve().parents[2] / "src" / "autoscreener"
    forbidden = ("event_calendar", "EventCalendar", "collect_events", "calendar_source")
    offenders: list[str] = []
    for sub in ("scoring", "backtest"):
        for path in (root / sub).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"PIT汚染の疑い: {offenders}"


# --- バッチ(DB) -----------------------------------------------------------


@pytest.fixture
def seeded_event_ticker():
    symbol = "ZZEVT1"
    with session_scope() as session:
        existing = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if existing is not None:
            session.query(EventCalendar).filter_by(ticker_id=existing.id).delete()
            session.delete(existing)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
    yield symbol
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if t is not None:
            session.query(EventCalendar).filter_by(ticker_id=t.id).delete()
            session.delete(t)


def test_collect_events_creates_no_row_for_empty_or_past_calendar(seeded_event_ticker):
    counts = collect_events(
        symbols=[seeded_event_ticker], as_of=_TODAY, calendar_fetcher=lambda _s: {}
    )
    assert counts["new_events"] == 0
    assert counts["no_date"] == 1

    counts = collect_events(
        symbols=[seeded_event_ticker],
        as_of=_TODAY,
        calendar_fetcher=lambda _s: {"Earnings Date": [datetime.date(2026, 1, 1)]},
    )
    assert counts["new_events"] == 0


def test_collect_events_is_idempotent_on_repeat(seeded_event_ticker):
    fetcher = lambda _s: {"Earnings Date": [datetime.date(2026, 11, 5)]}  # noqa: E731
    first = collect_events(symbols=[seeded_event_ticker], as_of=_TODAY, calendar_fetcher=fetcher)
    second = collect_events(symbols=[seeded_event_ticker], as_of=_TODAY, calendar_fetcher=fetcher)
    assert first["new_events"] == 1
    assert second["new_events"] == 0
    assert second["existing"] == 1
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=seeded_event_ticker).one()
        rows = session.query(EventCalendar).filter_by(ticker_id=t.id).all()
        assert len(rows) == 1
        assert rows[0].event_type == "earnings"
        assert rows[0].collected_on == _TODAY
