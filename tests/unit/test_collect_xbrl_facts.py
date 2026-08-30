"""tests/unit/test_collect_xbrl_facts.py(30.5.6)。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from autoscreener.batch.collect_xbrl_facts import collect_xbrl_facts
from autoscreener.config import EdgarConfig, EdgarRetryConfig
from autoscreener.db.models import Ticker, XbrlFact
from autoscreener.db.session import session_scope

_SYMBOL = "ZZXBRL1"

_COMPANY_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "end": "2026-06-30",
                            "val": 1000000,
                            "form": "10-Q",
                            "accn": "0001234567-26-000001",
                            "filed": "2026-07-15",
                        }
                    ]
                }
            }
        }
    }
}


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
            session.query(XbrlFact).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture
def ticker_with_cik():
    _cleanup()
    with session_scope() as session:
        session.add(Ticker(symbol=_SYMBOL, market="US", cik="0000320193"))
    yield _SYMBOL
    _cleanup()


def test_collect_xbrl_facts_inserts_and_dedupes_on_rerun(ticker_with_cik):
    with (
        patch("autoscreener.batch.collect_xbrl_facts.EdgarClient") as mock_client_cls,
        patch("autoscreener.batch.collect_xbrl_facts.get_settings") as mock_settings,
        patch("autoscreener.batch.collect_xbrl_facts.load_edgar_config", return_value=_edgar_config()),
    ):
        mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
        mock_client_cls.return_value.fetch_company_facts.return_value = _COMPANY_FACTS

        counts_1 = collect_xbrl_facts(symbols=[ticker_with_cik])
        counts_2 = collect_xbrl_facts(symbols=[ticker_with_cik])

    assert counts_1["facts_upserted"] == 1
    assert counts_2["facts_upserted"] == 0  # 重複はupsertでスキップ

    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=ticker_with_cik).one()
        rows = session.query(XbrlFact).filter_by(ticker_id=ticker.id).all()
        assert len(rows) == 1
        assert float(rows[0].value) == 1000000


def test_collect_xbrl_facts_skipped_no_cik():
    symbol = "ZZXBRL2"
    with session_scope() as session:
        session.query(Ticker).filter_by(symbol=symbol).delete()
        session.add(Ticker(symbol=symbol, market="US", cik=None))
    try:
        with (
            patch("autoscreener.batch.collect_xbrl_facts.EdgarClient") as mock_client_cls,
            patch("autoscreener.batch.collect_xbrl_facts.get_settings") as mock_settings,
            patch("autoscreener.batch.collect_xbrl_facts.load_edgar_config", return_value=_edgar_config()),
        ):
            mock_settings.return_value.edgar_user_agent = "TENX research <test@example.com>"
            counts = collect_xbrl_facts(symbols=[symbol])
        assert counts["skipped_no_cik"] == 1
    finally:
        with session_scope() as session:
            session.query(Ticker).filter_by(symbol=symbol).delete()
