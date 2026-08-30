"""tests/unit/test_collect_guidance.py(K-6)。

DBに触れるテストはローカル開発用Postgres(`docker compose up -d`)に対して
実行する。専用シンボル(ZZ***)を使い、終了時に削除する
(`tests/unit/test_collect_filings.py` と同じ方針)。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch.collect_guidance import collect_guidance
from autoscreener.dates import utc_today
from autoscreener.db.models import FilingSection, Guidance, Ticker
from autoscreener.db.session import session_scope

_SYMBOL = "ZZGUI1"


def _cleanup():
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is not None:
            session.query(Guidance).filter_by(ticker_id=ticker.id).delete()
            session.query(FilingSection).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


@pytest.fixture
def ticker_id():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US")
        session.add(ticker)
        session.flush()
        tid = ticker.id
    yield tid
    _cleanup()


def _add_ex99_section(ticker_id: int, text: str, *, accession: str = "0001234567-26-000099") -> None:
    with session_scope() as session:
        session.add(
            FilingSection(
                ticker_id=ticker_id,
                accession_number=accession,
                form="8-K",
                filed_date=datetime.date(2026, 8, 1),
                section="ex99",
                text=text,
                char_count=len(text),
                source_url="https://www.sec.gov/Archives/edgar/data/1/000123456726000099/ex991.htm",
                extracted_on=utc_today(),
            )
        )


def test_collect_guidance_extracts_and_upserts(ticker_id):
    _add_ex99_section(
        ticker_id, "We expect revenue of $120 million to $125 million for fiscal 2027."
    )
    counts = collect_guidance(symbols=[_SYMBOL])
    assert counts["tickers"] == 1
    assert counts["sections"] == 1
    assert counts["new_items"] == 1
    assert counts["failures"] == 0

    with session_scope() as session:
        rows = session.query(Guidance).filter_by(ticker_id=ticker_id).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.metric == "revenue"
    assert row.period_label == "FY2027"
    assert float(row.low_usd) == 120_000_000.0
    assert float(row.high_usd) == 125_000_000.0
    assert row.raw_text is not None


def test_collect_guidance_is_idempotent_on_rerun(ticker_id):
    _add_ex99_section(
        ticker_id, "We expect revenue of $120 million to $125 million for fiscal 2027."
    )
    counts_1 = collect_guidance(symbols=[_SYMBOL])
    counts_2 = collect_guidance(symbols=[_SYMBOL])
    assert counts_1["new_items"] == 1
    assert counts_2["new_items"] == 0
    assert counts_2["existing"] == 1


def test_collect_guidance_no_sections_produces_no_items(ticker_id):
    counts = collect_guidance(symbols=[_SYMBOL])
    assert counts["tickers"] == 1
    assert counts["sections"] == 0
    assert counts["new_items"] == 0
