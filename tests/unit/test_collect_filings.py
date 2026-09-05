"""tests/unit/test_collect_filings.py(30.3.7)。

DBに触れるテストはローカル開発用Postgres(`docker compose up -d`)に対して実行する。
専用シンボル(ZZ***)を使い、終了時に削除する。
"""

from __future__ import annotations

import datetime
from unittest.mock import ANY, patch

import pytest

from autoscreener.batch.collect_filings import (
    POST_DELISTING_FILING_WINDOW_DAYS,
    TRACKED_FORMS,
    collect_filings,
    select_tracked_tickers,
)
from autoscreener.collectors.edgar_client import FilingRecord
from autoscreener.collectors.errors import EmptyResponseError, TransientFailure
from autoscreener.config import EdgarConfig, EdgarRetryConfig
from autoscreener.db.models import CollectionCursor, Filing, Score, Ticker
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


# --- 2026-09-05: post-delisting evidence window ----------------------------
# docs/delisting_label_backfill_2026-09-04.md §3 / docs/post_delisting_evidence_collection_2026-09-05.md
# Form 25/15, 8-K Item 1.03/2.01/3.01, DEFM14A and SC 13E-3 are filed at and
# after `delisted_at`; the collector must keep pulling for a bounded window
# instead of dropping the CIK the instant it delists.


def test_tracked_forms_include_post_delisting_evidence_forms():
    """These were entirely absent before 2026-09-05 (SC 13E3/DEFM14A) or only
    partially covered (Form 25 bare form / 15-12G / foreign-issuer variants),
    per docs/delisting_label_backfill_2026-09-04.md §3."""
    for form in ("25", "15-12G", "15F-12B", "15F-12G", "SC 13E3", "SC 13E-3", "DEFM14A"):
        assert form in TRACKED_FORMS, form
    # Pre-existing forms must not have been dropped by the edit.
    for form in ("8-K", "25-NSE", "15-12B", "DEF 14A"):
        assert form in TRACKED_FORMS, form


def _make_ticker(symbol: str, *, cik: str, delisted_days_ago: int | None = None) -> Ticker:
    delisted_at = None
    if delisted_days_ago is not None:
        delisted_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=delisted_days_ago)
    return Ticker(symbol=symbol, market="US", cik=cik, delisted_at=delisted_at)


def _cleanup_symbols(symbols: list[str]) -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
        ids = [t.id for t in tickers]
        if ids:
            session.query(Filing).filter(Filing.ticker_id.in_(ids)).delete(synchronize_session=False)
            session.query(Score).filter(Score.ticker_id.in_(ids)).delete(synchronize_session=False)
        for t in tickers:
            session.delete(t)


def test_select_tracked_tickers_includes_recently_delisted_within_window_and_excludes_outside_it():
    """The window boundary: a ticker delisted just inside
    POST_DELISTING_FILING_WINDOW_DAYS must be pulled in; one delisted well
    before the window (e.g. one of the 94 stale events from before this fix)
    must not be — it stays reliant on whatever `filings` it already has."""
    always_tracked = "ZZDELWIN"  # kept via positions so the union branch (not the
    # active/SEC-mapped bootstrap fallback) is exercised even if both delisted
    # candidates below were excluded.
    within_window = "ZZDELRECENT"
    outside_window = "ZZDELOLD"
    symbols = [always_tracked, within_window, outside_window]
    _cleanup_symbols(symbols)
    try:
        with session_scope() as session:
            session.add(_make_ticker(always_tracked, cik="0000900201"))
            session.add(_make_ticker(within_window, cik="0000900202", delisted_days_ago=10))
            session.add(_make_ticker(
                outside_window, cik="0000900203",
                delisted_days_ago=POST_DELISTING_FILING_WINDOW_DAYS + 30,
            ))

        with (
            patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
            patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
        ):
            mock_positions.return_value.positions = [
                type("P", (), {"ticker": always_tracked, "closed_on": None})()
            ]
            with session_scope() as session:
                tickers = select_tracked_tickers(session, limit=300)

        symbols_returned = {t.symbol for t in tickers}
        assert always_tracked in symbols_returned
        assert within_window in symbols_returned, "just-delisted ticker must still be collected"
        assert outside_window not in symbols_returned, "a stale delisting must not reopen collection forever"
    finally:
        _cleanup_symbols(symbols)


def test_select_tracked_tickers_excludes_delisted_ticker_sharing_cik_with_active_ticker():
    """Regression for the CIK-collision trap (docs/delisting_label_backfill_2026-09-04.md
    §2: active TDW and delisted TDGMW share one CIK). If a delisted ticker's CIK is
    still held by an active ticker, `EdgarClient.fetch_filings(cik)` would return the
    active ticker's ordinary ongoing filings — pulling those in under the delisted
    ticker's own ticker_id would corrupt `filings`, not just confuse a downstream join."""
    always_tracked = "ZZDELWIN2"
    active_symbol = "ZZDELACTIVE"
    shared_cik_symbol = "ZZDELSHARED"
    shared_cik = "0000900210"
    symbols = [always_tracked, active_symbol, shared_cik_symbol]
    _cleanup_symbols(symbols)
    try:
        with session_scope() as session:
            session.add(_make_ticker(always_tracked, cik="0000900211"))
            session.add(_make_ticker(active_symbol, cik=shared_cik))  # delisted_at is None
            session.add(_make_ticker(shared_cik_symbol, cik=shared_cik, delisted_days_ago=5))

        with (
            patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
            patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
        ):
            mock_positions.return_value.positions = [
                type("P", (), {"ticker": always_tracked, "closed_on": None})()
            ]
            with session_scope() as session:
                tickers = select_tracked_tickers(session, limit=300)

        symbols_returned = {t.symbol for t in tickers}
        assert shared_cik_symbol not in symbols_returned, (
            "a delisted ticker sharing its CIK with an active ticker must not be "
            "auto-tracked — fetch_filings(cik) would return the active ticker's own "
            "filings and misattribute them under the delisted ticker's ticker_id"
        )
    finally:
        _cleanup_symbols(symbols)


def test_collect_filings_attributes_post_delisting_filings_to_the_delisted_tickers_own_ticker_id():
    """A delisted ticker (e.g. selected via the window in
    `select_tracked_tickers`) must have its newly-tracked forms (Form 25 here)
    land under its own `ticker_id` — the same `_upsert_filings` path used for
    any other tracked ticker, exercised with an explicit `symbols=[...]` call
    so this test does not depend on (or pollute) the rest of the shared test DB
    the way calling the default `select_tracked_tickers`-driven path would."""
    symbol = "ZZDELE2E"
    _cleanup_symbols([symbol])
    try:
        with session_scope() as session:
            session.add(_make_ticker(symbol, cik="0000900220", delisted_days_ago=15))

        record = FilingRecord(
            accession_number="0000900220-26-000001",
            form="25",
            filed_date=datetime.date.today() - datetime.timedelta(days=15),
            report_date=None,
            items=[],
            primary_document="form25.htm",
            document_url="https://www.sec.gov/example/form25.htm",
        )
        with (
            patch("autoscreener.batch.collect_filings.EdgarClient") as mock_client_cls,
            patch("autoscreener.batch.collect_filings.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_filings.load_edgar_config", return_value=_edgar_config()),
        ):
            mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
            mock_client_cls.return_value.fetch_filings.return_value = [record]

            counts = collect_filings(symbols=[symbol])

        assert counts["changed_symbols"] == [symbol]
        mock_client_cls.return_value.fetch_filings.assert_called_once_with("0000900220", forms=TRACKED_FORMS)
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            filing = session.query(Filing).filter_by(ticker_id=ticker.id).one()
            assert filing.form == "25"
            assert filing.accession_number == "0000900220-26-000001"
    finally:
        _cleanup_symbols([symbol])


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


def _clear_daily_index_cursor() -> None:
    with session_scope() as session:
        session.query(CollectionCursor).filter_by(
            source="sec_edgar", scope="tracked_filings_daily_index"
        ).delete()


def test_daily_index_fetches_only_changed_ciks_and_advances_cursor(ticker_with_cik):
    as_of = datetime.date(2026, 9, 5)
    record = FilingRecord(
        accession_number="0001234567-26-000099",
        form="8-K",
        filed_date=datetime.date(2026, 9, 3),
        report_date=None,
        items=["2.02"],
        primary_document="earnings.htm",
        document_url="https://www.sec.gov/example/earnings.htm",
    )
    _clear_daily_index_cursor()
    try:
        with (
            patch("autoscreener.batch.collect_filings.EdgarClient") as mock_client_cls,
            patch("autoscreener.batch.collect_filings.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
            patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
        ):
            mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
            mock_positions.return_value.positions = []

            def _index(day, *, forms):
                if day == datetime.date(2026, 9, 3):
                    return {"0000320193"}
                raise EmptyResponseError("no index for calendar day")

            mock_client_cls.return_value.fetch_daily_index_ciks.side_effect = _index
            mock_client_cls.return_value.fetch_filings.return_value = [record]
            counts = collect_filings(
                symbols=[ticker_with_cik],
                edgar_config=_edgar_config(),
                use_daily_index=True,
                as_of=as_of,
            )

        assert counts["tickers"] == 1
        assert counts["new_filings"] == 1
        assert counts["changed_symbols"] == [ticker_with_cik]
        mock_client_cls.return_value.fetch_filings.assert_called_once_with(
            "0000320193", forms=ANY
        )
        requested_index_dates = {
            call.args[0] for call in mock_client_cls.return_value.fetch_daily_index_ciks.call_args_list
        }
        assert all(day.weekday() < 5 for day in requested_index_dates)
        with session_scope() as session:
            cursor = session.query(CollectionCursor).filter_by(
                source="sec_edgar", scope="tracked_filings_daily_index"
            ).one()
            assert cursor.cursor_date == datetime.date(2026, 9, 3)
    finally:
        _clear_daily_index_cursor()


def test_daily_index_cursor_does_not_advance_when_ticker_fetch_fails(ticker_with_cik):
    as_of = datetime.date(2026, 9, 5)
    _clear_daily_index_cursor()
    try:
        with (
            patch("autoscreener.batch.collect_filings.EdgarClient") as mock_client_cls,
            patch("autoscreener.batch.collect_filings.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_filings.load_positions_config") as mock_positions,
            patch("autoscreener.batch.collect_filings.load_all_notes", return_value={}),
        ):
            mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
            mock_positions.return_value.positions = []
            mock_client_cls.return_value.fetch_daily_index_ciks.return_value = {"0000320193"}
            mock_client_cls.return_value.fetch_filings.side_effect = TransientFailure("retry later")
            counts = collect_filings(
                symbols=[ticker_with_cik],
                edgar_config=_edgar_config(),
                use_daily_index=True,
                as_of=as_of,
            )
        assert counts["failures"] == 1
        with session_scope() as session:
            assert session.query(CollectionCursor).filter_by(
                source="sec_edgar", scope="tracked_filings_daily_index"
            ).one_or_none() is None
    finally:
        _clear_daily_index_cursor()
