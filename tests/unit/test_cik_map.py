"""tests/unit/test_cik_map.py(30.3.7)。

`refresh_cik_map` のDB書き込みを検証するテストは、他のDB系テストと同じく
ローカル開発用Postgres(`docker compose up -d`)に対して実行する。専用の
ティッカーシンボル(ZZ***)を使い、終了時に削除する。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from autoscreener.batch.refresh_cik_map import _build_reverse_map, refresh_cik_map
from autoscreener.db.models import Ticker
from autoscreener.db.session import session_scope


def test_build_reverse_map_expands_variants():
    resolved, ambiguous = _build_reverse_map({"BRK.B": "0001234567"})
    assert resolved["BRK.B"] == "0001234567"
    assert resolved["BRK-B"] == "0001234567"
    assert ambiguous == set()


def test_build_reverse_map_detects_ambiguity():
    # BRK.B と BRK-B が別々のCIKに割り当てられている(データ不整合の例)
    resolved, ambiguous = _build_reverse_map({"BRK.B": "111", "BRK-B": "222"})
    assert "BRK.B" in ambiguous
    assert "BRK-B" in ambiguous
    assert resolved == {}


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        session.query(Ticker).filter(Ticker.symbol.in_(symbols)).delete(synchronize_session=False)


@pytest.fixture
def matched_ticker():
    symbol = "ZZCIK1"
    _cleanup([symbol])
    with session_scope() as session:
        session.add(Ticker(symbol=symbol, market="US"))
    yield symbol
    _cleanup([symbol])


@pytest.fixture
def unmatched_ticker():
    symbol = "ZZCIK2"
    _cleanup([symbol])
    with session_scope() as session:
        session.add(Ticker(symbol=symbol, market="US"))
    yield symbol
    _cleanup([symbol])


def test_refresh_cik_map_updates_known_ticker(matched_ticker):
    with (
        patch("autoscreener.batch.refresh_cik_map.EdgarClient") as mock_client_cls,
        patch("autoscreener.batch.refresh_cik_map.get_settings") as mock_settings,
        patch("autoscreener.batch.refresh_cik_map.load_edgar_config"),
    ):
        mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
        mock_client_cls.return_value.fetch_company_tickers.return_value = {matched_ticker: "0000320193"}
        with session_scope() as session:
            counts = refresh_cik_map(session)

    assert counts["matched"] == 1
    assert counts["updated"] == 1
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=matched_ticker).one()
        assert ticker.cik == "0000320193"


def test_refresh_cik_map_leaves_unmatched_ticker_alone(unmatched_ticker):
    with (
        patch("autoscreener.batch.refresh_cik_map.EdgarClient") as mock_client_cls,
        patch("autoscreener.batch.refresh_cik_map.get_settings") as mock_settings,
        patch("autoscreener.batch.refresh_cik_map.load_edgar_config"),
    ):
        mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
        mock_client_cls.return_value.fetch_company_tickers.return_value = {"AAPL": "0000320193"}
        with session_scope() as session:
            counts = refresh_cik_map(session)

    assert counts["unmatched"] >= 1
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=unmatched_ticker).one()
        assert ticker.cik is None
