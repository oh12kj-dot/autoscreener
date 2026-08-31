"""J-7(docs/investment_decision_gap_2026-08-29.md):需給の収集と隔離のテスト。"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from autoscreener.batch.collect_supply import (
    InsiderRow,
    collect_insider,
    collect_short_interest,
)
from autoscreener.collectors.short_interest_source import ShortInterestRecord
from autoscreener.db.models import InsiderTransaction, ShortInterest, Ticker
from autoscreener.db.session import session_scope

_TODAY = datetime.date(2026, 8, 29)


@pytest.fixture
def seeded_supply_ticker():
    symbol = "ZZSUP1"
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if t is not None:
            session.query(InsiderTransaction).filter_by(ticker_id=t.id).delete()
            session.query(ShortInterest).filter_by(ticker_id=t.id).delete()
            session.delete(t)
        session.add(Ticker(symbol=symbol, market="US"))
    yield symbol
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if t is not None:
            session.query(InsiderTransaction).filter_by(ticker_id=t.id).delete()
            session.query(ShortInterest).filter_by(ticker_id=t.id).delete()
            session.delete(t)


def test_collect_insider_is_idempotent(seeded_supply_ticker):
    rows = [
        InsiderRow(
            accession_number="0000-1",
            transaction_date=datetime.date(2026, 8, 1),
            insider_name="Jane Doe",
            transaction_code="P",
            shares=1000.0,
            price_usd=10.0,
        )
    ]
    first = collect_insider(symbols=[seeded_supply_ticker], fetcher=lambda _t: rows)
    second = collect_insider(symbols=[seeded_supply_ticker], fetcher=lambda _t: rows)
    assert first["new_rows"] == 1
    assert second["new_rows"] == 0
    assert second["existing"] == 1
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=seeded_supply_ticker).one()
        assert session.query(InsiderTransaction).filter_by(ticker_id=t.id).count() == 1


def test_collect_short_interest_is_idempotent(seeded_supply_ticker):
    record = ShortInterestRecord(
        settlement_date=datetime.date(2026, 8, 15),
        symbol=seeded_supply_ticker,
        current_short_shares=50000.0,
        previous_short_shares=40000.0,
        avg_daily_volume=10000.0,
        reported_days_to_cover=5.0,
    )
    first = collect_short_interest(symbols=[seeded_supply_ticker], fetcher=lambda _t: [record], as_of=_TODAY)
    second = collect_short_interest(symbols=[seeded_supply_ticker], fetcher=lambda _t: [record], as_of=_TODAY)
    assert first["new_rows"] == 1
    assert second["new_rows"] == 0
    with session_scope() as session:
        t = session.query(Ticker).filter_by(symbol=seeded_supply_ticker).one()
        row = session.query(ShortInterest).filter_by(ticker_id=t.id).one()
        assert float(row.days_to_cover) == 5.0
        assert row.published_date == _TODAY


def test_gates_and_scoring_never_import_supply_tables():
    """原則3:需給はゲート・スコアに入れない。`screening/exclusion_gates.py` /
    `screening/*gate*` と `scoring/` のソースに需給テーブルの文字列が現れないこと。"""
    root = Path(__file__).resolve().parents[2] / "src" / "autoscreener"
    forbidden = ("InsiderTransaction", "ShortInterest", "collect_supply", "short_interest_source")
    offenders: list[str] = []
    targets = list((root / "scoring").rglob("*.py"))
    targets += [root / "screening" / "exclusion_gates.py"]
    targets += list((root / "batch").glob("apply_gates.py"))
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"原則3違反の疑い: {offenders}"
