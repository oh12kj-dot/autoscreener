"""tests/unit/test_collect_filings.py(30.3.7)。

DBに触れるテストはローカル開発用Postgres(`docker compose up -d`)に対して実行する。
専用シンボル(ZZ***)を使い、終了時に削除する。
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from autoscreener.batch.collect_filings import collect_filings, select_tracked_tickers
from autoscreener.collectors.edgar_client import FilingRecord
from autoscreener.config import EdgarConfig, EdgarRetryConfig
from autoscreener.db.models import Filing, Score, Ticker
from autoscreener.db.session import session_scope

_SYMBOL = "ZZFIL1"


def _edgar_config() -> EdgarConfig:
    return EdgarConfig(
        enabled=True,
        requests_per_second=10.0,
        timeout_seconds=5.0,
        document_fetch_enabled=True,
        max_tracked_tickers=300,
        retry=EdgarRetryConfig(max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.02),
    )


def _cleanup():
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
            session.query(Score).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture
def ticker_with_cik():
    _cleanup()
    with session_scope() as session:
        session.add(Ticker(symbol=_SYMBOL, market="US", cik="0000320193"))
    yield _SYMBOL
    _cleanup()


def test_select_tracked_tickers_includes_position_holdings(ticker_with_cik):
    with (
        patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
        patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
    ):
        mock_positions.return_value.positions = [
            type("P", (), {"ticker": _SYMBOL, "closed_on": None})()
        ]
        with session_scope() as session:
            tickers = select_tracked_tickers(session, limit=300)
    assert any(t.symbol == _SYMBOL for t in tickers)


def test_select_tracked_tickers_excludes_ticker_without_cik():
    symbol = "ZZFIL2"
    with session_scope() as session:
        session.query(Ticker).filter_by(symbol=symbol).delete()
        session.add(Ticker(symbol=symbol, market="US", cik=None))
    try:
        with (
            patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
            patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
        ):
            mock_positions.return_value.positions = [type("P", (), {"ticker": symbol, "closed_on": None})()]
            with session_scope() as session:
                tickers = select_tracked_tickers(session, limit=300)
        assert not any(t.symbol == symbol for t in tickers)
    finally:
        with session_scope() as session:
            session.query(Ticker).filter_by(symbol=symbol).delete()


def test_select_tracked_tickers_falls_back_to_active_sec_mapped_universe(ticker_with_cik):
    with (
        patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
        patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
    ):
        mock_positions.return_value.positions = []
        with session_scope() as session:
            tickers = select_tracked_tickers(session, limit=300)
    assert tickers
    assert all(t.cik is not None for t in tickers)


def test_collect_filings_inserts_new_and_skips_duplicates_on_rerun(ticker_with_cik):
    records = [
        FilingRecord(
            accession_number="0001234567-26-000001",
            form="8-K",
            filed_date=datetime.date(2026, 8, 1),
            report_date=None,
            items=["4.02"],
            primary_document="a.htm",
            document_url="https://www.sec.gov/Archives/edgar/data/320193/000123456726000001/a.htm",
        )
    ]
    with (
        patch("autoscreener.batch.collect_filings.EdgarClient") as mock_client_cls,
        patch("autoscreener.batch.collect_filings.get_settings") as mock_settings,
        patch("autoscreener.batch.collect_filings.load_edgar_config", return_value=_edgar_config()),
    ):
        mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
        mock_client_cls.return_value.fetch_filings.return_value = records

        counts_1 = collect_filings(symbols=[ticker_with_cik])
        counts_2 = collect_filings(symbols=[ticker_with_cik])

    assert counts_1["new_filings"] == 1
    assert counts_2["new_filings"] == 0  # 2回目は重複なので0件


def test_collect_filings_counts_skipped_no_cik():
    symbol = "ZZFIL3"
    with session_scope() as session:
        session.query(Ticker).filter_by(symbol=symbol).delete()
        session.add(Ticker(symbol=symbol, market="US", cik=None))
    try:
        with (
            patch("autoscreener.batch.collect_filings.EdgarClient") as mock_client_cls,
            patch("autoscreener.batch.collect_filings.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_filings.load_edgar_config", return_value=_edgar_config()),
        ):
            mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
            counts = collect_filings(symbols=[symbol])
        assert counts["skipped_no_cik"] == 1
        mock_client_cls.return_value.fetch_filings.assert_not_called()
    finally:
        with session_scope() as session:
            session.query(Ticker).filter_by(symbol=symbol).delete()


def test_collect_filings_disabled_returns_zero_counts():
    disabled = _edgar_config()
    disabled = disabled.model_copy(update={"enabled": False})
    with patch("autoscreener.batch.collect_filings.get_settings") as mock_settings:
        mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
        counts = collect_filings(edgar_config=disabled)
    assert counts == {"tickers": 0, "new_filings": 0, "skipped_no_cik": 0, "failures": 0}
