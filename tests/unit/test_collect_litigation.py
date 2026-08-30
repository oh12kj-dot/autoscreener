"""tests/unit/test_collect_litigation.py(K-5)。

DBに触れるテストはローカル開発用Postgres(`docker compose up -d`)に対して
実行する。専用シンボル(ZZ***)を使い、終了時に削除する。`fetcher` を注入して
全文検索APIへは一切出ない。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch.collect_litigation import collect_litigation
from autoscreener.collectors.litigation_source import LitigationHit
from autoscreener.dates import utc_today
from autoscreener.db.models import FilingSection, LitigationEvent, Ticker
from autoscreener.db.session import session_scope

_SYMBOL = "ZZLIT9"


def _cleanup():
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(LitigationEvent).filter_by(ticker_id=ticker.id).delete()
            session.query(FilingSection).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture
def ticker_id():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US", cik="0000320193")
        session.add(ticker)
        session.flush()
        tid = ticker.id
    yield tid
    _cleanup()


def _empty_fetcher(_ticker, _kinds):
    return []


def test_collect_litigation_extracts_from_item3_and_skips_fts_for_that_kind(ticker_id):
    text = (
        "Item 3. Legal Proceedings\n\nOn March 12, 2026, a putative securities "
        "class action was filed against the Company in the Southern District "
        "of New York."
    )
    with session_scope() as session:
        session.add(
            FilingSection(
                ticker_id=ticker_id,
                accession_number="0001234567-26-000050",
                form="10-K",
                filed_date=datetime.date(2026, 3, 15),
                section="item3",
                text=text,
                char_count=len(text),
                source_url="https://www.sec.gov/Archives/edgar/data/1/x/item3.htm",
                extracted_on=utc_today(),
            )
        )

    calls: list[frozenset[str]] = []

    def _tracking_fetcher(_ticker, kinds):
        calls.append(kinds)
        return []

    counts = collect_litigation(symbols=[_SYMBOL], fetcher=_tracking_fetcher)
    assert counts["tickers"] == 1
    assert counts["new_events"] == 1
    assert counts["failures"] == 0

    # class_action は item3 で見つかったので、全文検索の補助クエリからは除外される。
    assert calls and "class_action" not in calls[0]
    assert "short_report" in calls[0]

    with session_scope() as session:
        rows = session.query(LitigationEvent).filter_by(ticker_id=ticker_id).all()
    assert len(rows) == 1
    assert rows[0].kind == "class_action"
    assert "putative securities class action" in rows[0].detail


def test_collect_litigation_uses_fetcher_when_no_item3(ticker_id):
    hit = LitigationHit(
        kind="short_report",
        title="8-K EXAMPLE CORP",
        event_date=datetime.date(2026, 7, 8),
        source_url="https://www.sec.gov/Archives/edgar/data/1/y/ex991.htm",
        source_accession="0001234567-26-000077",
        detail=None,
    )

    def _fetcher(_ticker, _kinds):
        return [hit]

    counts = collect_litigation(symbols=[_SYMBOL], fetcher=_fetcher)
    assert counts["new_events"] == 1

    with session_scope() as session:
        rows = session.query(LitigationEvent).filter_by(ticker_id=ticker_id).all()
    assert len(rows) == 1
    assert rows[0].kind == "short_report"
    assert rows[0].source_accession == "0001234567-26-000077"


def test_collect_litigation_idempotent_on_rerun(ticker_id):
    hit = LitigationHit(
        kind="sec_investigation",
        title="8-K EXAMPLE CORP",
        event_date=datetime.date(2026, 6, 1),
        source_url=None,
        source_accession="0001234567-26-000010",
        detail=None,
    )

    def _fetcher(_ticker, _kinds):
        return [hit]

    counts_1 = collect_litigation(symbols=[_SYMBOL], fetcher=_fetcher)
    counts_2 = collect_litigation(symbols=[_SYMBOL], fetcher=_fetcher)
    assert counts_1["new_events"] == 1
    assert counts_2["new_events"] == 0
    assert counts_2["existing"] == 1


def test_collect_litigation_no_hits_when_fetcher_empty(ticker_id):
    counts = collect_litigation(symbols=[_SYMBOL], fetcher=_empty_fetcher)
    assert counts["tickers"] == 1
    assert counts["new_events"] == 0
