import datetime

import pytest

from autoscreener.batch.market_session import assess_market_session, latest_completed_us_session
from autoscreener.db.models import PriceSnapshot, Ticker
from autoscreener.db.session import session_scope


_SYMBOL_PREFIX = "ZZMKT"


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.like(f"{_SYMBOL_PREFIX}%")).all()
        for ticker in tickers:
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture(autouse=True)
def _isolated_market_rows():
    _cleanup()
    yield
    _cleanup()


def test_latest_completed_session_handles_jst_saturday_and_us_holiday():
    # 09:00 JST Saturday is Friday 20:00 ET during daylight time: Friday close is complete.
    assert latest_completed_us_session(
        datetime.datetime(2026, 9, 5, 0, 0, tzinfo=datetime.UTC)
    ) == datetime.date(2026, 9, 4)
    # Labor Day Monday is closed; Tuesday 09:00 JST still points to Friday's session.
    assert latest_completed_us_session(
        datetime.datetime(2026, 9, 8, 0, 0, tzinfo=datetime.UTC)
    ) == datetime.date(2026, 9, 4)


def test_partial_market_session_collects_only_missing_symbols():
    with session_scope() as session:
        tickers = [Ticker(symbol=f"{_SYMBOL_PREFIX}{i}", market="US") for i in range(10)]
        session.add_all(tickers)
        session.flush()
        for ticker in tickers[:9]:
            session.add(PriceSnapshot(ticker_id=ticker.id, trade_date=datetime.date(2026, 9, 4)))
        session.flush()
        decision = assess_market_session(
            session,
            target_count=10,
            minimum_coverage=0.9,
            symbols=[ticker.symbol for ticker in tickers],
            now=datetime.datetime(2026, 9, 8, 0, 0, tzinfo=datetime.UTC),
        )
    assert decision.should_run is True
    assert decision.latest_covered_session == datetime.date(2026, 9, 4)
    assert decision.covered_count == 9
    assert decision.symbols_to_collect == (f"{_SYMBOL_PREFIX}9",)


def test_complete_market_session_skips_collection():
    with session_scope() as session:
        tickers = [Ticker(symbol=f"{_SYMBOL_PREFIX}{i}", market="US") for i in range(10)]
        session.add_all(tickers)
        session.flush()
        for ticker in tickers:
            session.add(PriceSnapshot(ticker_id=ticker.id, trade_date=datetime.date(2026, 9, 4)))
        session.flush()
        decision = assess_market_session(
            session,
            target_count=10,
            minimum_coverage=0.9,
            symbols=[ticker.symbol for ticker in tickers],
            now=datetime.datetime(2026, 9, 8, 0, 0, tzinfo=datetime.UTC),
        )
    assert decision.should_run is False
    assert decision.covered_count == 10
    assert decision.symbols_to_collect == ()


def test_one_future_row_does_not_suppress_collection():
    with session_scope() as session:
        ticker = Ticker(symbol=f"{_SYMBOL_PREFIX}FUT", market="US")
        session.add(ticker)
        session.flush()
        session.add(PriceSnapshot(ticker_id=ticker.id, trade_date=datetime.date(2026, 9, 9)))
        session.flush()
        decision = assess_market_session(
            session,
            target_count=10,
            minimum_coverage=0.9,
            symbols=[ticker.symbol],
            now=datetime.datetime(2026, 9, 8, 0, 0, tzinfo=datetime.UTC),
        )
    assert decision.should_run is True
