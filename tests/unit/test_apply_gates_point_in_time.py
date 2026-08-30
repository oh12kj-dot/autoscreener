"""ゲート入力がポイントインタイムであることのテスト(14.3)。

`docker compose up -d` で起動済みのローカル開発用Postgresに対して実行する
(test_api_routes.py と同じ方針)。専用シンボル(ZZ***)を使い、後片付けする。

**なぜこのテストが要るか(2026-08-26に発見した欠陥)。** `apply_gates(date)` は
CLIが「`--date` で過去日を指定できる」と案内しているのに、実装は日付に関係なく
「最新の raw_snapshot」と「price_snapshots の全行の末尾90行」を読んでいた。
つまり過去日の `universe_snapshots` を**今日のデータで**書き直していた。
スコアリング側は `as_of` で厳密に切っているため、同じ日付の「ゲート判定」と
「スコア」が別の時点の情報で作られるという不整合にもなっていた。

`raw_snapshots.available_from` は 14.3(先読みバイアス対策)のために用意された
列だが、この修正まで**どのクエリからも参照されていなかった**。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch.apply_gates import _gather_gate_input
from autoscreener.db.models import PriceSnapshot, RawSnapshot, Ticker
from autoscreener.db.session import session_scope

_SYMBOL = "ZZPIT1"
_OLD = datetime.date(2099, 1, 10)
_NEW = datetime.date(2099, 2, 10)


def _cleanup() -> None:
    with session_scope() as session:
        for ticker in session.query(Ticker).filter(Ticker.symbol == _SYMBOL).all():
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


def _snapshot(ticker_id: int, day: datetime.date, market_cap: float, suffix: str) -> RawSnapshot:
    return RawSnapshot(
        ticker_id=ticker_id,
        snapshot_date=day,
        source="test",
        payload={
            "info": {
                "marketCap": market_cap,
                "totalRevenue": 5.0e7,
                "currentPrice": 20.0,
                "sector": "Technology",
                "currency": "USD",
                "financialCurrency": "USD",
            },
            "balance_sheet": {"Stockholders Equity": {"2098-12-31": 1.0e8}},
            "quarterly_income_stmt": {
                "Total Revenue": {f"2098-{m:02d}-30": 1.0e7 for m in (3, 6, 9, 12)}
            },
        },
        content_hash=f"zzpit-{suffix}",
        last_seen_date=day,
        available_from=day,
        is_valid=True,
    )


@pytest.fixture
def seeded_ticker():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US", sector="Technology")
        session.add(ticker)
        session.flush()
        session.add(_snapshot(ticker.id, _OLD, market_cap=3.0e8, suffix="old"))
        session.add(_snapshot(ticker.id, _NEW, market_cap=9.0e8, suffix="new"))
        for offset, close in ((0, 20.0), (30, 50.0)):
            session.add(
                PriceSnapshot(
                    ticker_id=ticker.id,
                    trade_date=_OLD + datetime.timedelta(days=offset),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000,
                    shares_outstanding=15_000_000,
                )
            )
        ticker_id = ticker.id
    yield ticker_id
    _cleanup()


def test_gate_input_uses_the_snapshot_available_on_that_date(seeded_ticker):
    """過去日の判定には、その日に入手できていたスナップショットを使う。"""
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
        past = _gather_gate_input(session, ticker, _OLD)
        latest = _gather_gate_input(session, ticker, _NEW)
    assert past is not None and latest is not None
    assert past.market_cap == pytest.approx(3.0e8)
    assert latest.market_cap == pytest.approx(9.0e8)


def test_gate_input_ignores_prices_after_the_target_date(seeded_ticker):
    """流動性ゲートの売買代金中央値に、判定日より後の取引を混ぜない。"""
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
        past = _gather_gate_input(session, ticker, _OLD)
        latest = _gather_gate_input(session, ticker, _NEW)
    # _OLD 時点では 20.0 × 100万株のみ、_NEW 時点では 50.0 の行も入って中央値が上がる
    assert past.median_daily_dollar_volume == pytest.approx(20.0 * 1_000_000)
    assert latest.median_daily_dollar_volume == pytest.approx(35.0 * 1_000_000)


def test_gate_input_is_none_before_any_snapshot_was_available(seeded_ticker):
    """収集前の日付では「判定不能」になる(合格でも不合格でもない)。

    `apply_gates` はこれを `no_raw_data` として `included=False` に落とす。
    """
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one()
        assert _gather_gate_input(session, ticker, _OLD - datetime.timedelta(days=1)) is None
