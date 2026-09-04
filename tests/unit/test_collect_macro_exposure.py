"""tests/unit/test_collect_macro_exposure.py(S-6、docs/daily_pipeline_throughput_plan_2026-09-04.md)。

`collect_macro_exposure` は以前、ticker(実測299件)ループの**内側**で
factor(4件)ごとに同じ全系列クエリ(`MacroSeries`の全履歴)を毎回投げ直して
いた——クエリはどのtickerでも同じ結果を返すので、299×4=1,196回のうち
1,192回は無駄な再取得だった(13分の大半がこれ)。ここでは:

1. クエリをticker(・factor)ループの外へ括り出しても**出力が1ビットも
   変わらない**こと(素朴な再計算と突き合わせて確認)。
2. `MacroSeries`への問い合わせが、ticker数に比例せずfactor数だけで
   済んでいること(退行検知——再びループの中に戻したら失敗する)。

を確認する。DBに触れる(ローカル開発用Postgres、他のunitテストと同じ方針)。
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import event

from autoscreener.batch.collect_macro_exposure import _returns, _weekly_last, collect_macro_exposure
from autoscreener.db.models import (
    LiveDatasetCoverage,
    MacroExposureSnapshot,
    MacroSeries,
    PriceSnapshot,
    Ticker,
)
from autoscreener.db.session import get_engine, session_scope
from autoscreener.scoring.investment_intelligence import macro_exposure

_SYMBOLS = ["ZZMACROA", "ZZMACROB", "ZZMACROC"]
_FACTORS = ["ZZFACTOR1", "ZZFACTOR2"]
# 6週ぶんの月曜日(_weekly_lastはISO週の最終観測値を拾うので月曜1点/週で足りる)。
_WEEKS = [datetime.date(2098, 1, 5) + datetime.timedelta(weeks=i) for i in range(6)]


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(_SYMBOLS)).all()
        for ticker in tickers:
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(MacroExposureSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(LiveDatasetCoverage).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)
        session.query(MacroSeries).filter(MacroSeries.series_id.in_(_FACTORS)).delete(synchronize_session=False)


@pytest.fixture
def seeded() -> dict[str, int]:
    _cleanup()
    ticker_ids: dict[str, int] = {}
    with session_scope() as session:
        for t_idx, symbol in enumerate(_SYMBOLS):
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            ticker_ids[symbol] = ticker.id
            for w_idx, day in enumerate(_WEEKS):
                # tickerごとに値をずらし、betaがticker間で異なるようにする
                # (「たまたま同じ値だから区別できていない」を避けるため)。
                close = 100.0 + t_idx * 5.0 + w_idx * (3.0 + t_idx) + (1.5 if w_idx % 2 else -1.0)
                session.add(
                    PriceSnapshot(
                        ticker_id=ticker.id, trade_date=day, open=close, high=close,
                        low=close, close=close, volume=10_000, shares_outstanding=1_000_000,
                    )
                )
        for f_idx, factor in enumerate(_FACTORS):
            for w_idx, day in enumerate(_WEEKS):
                value = 2.0 + f_idx * 0.5 + w_idx * (0.2 + f_idx * 0.1) + (0.05 if w_idx % 2 else -0.03)
                session.add(MacroSeries(series_id=factor, observation_date=day, value=value))
    yield ticker_ids
    _cleanup()


def _naive_expected_beta(session, ticker_id: int, factor: str, as_of: datetime.date) -> float | None:
    """ループの外へ括り出す前と同じ計算を、tickerとfactorそれぞれ独立に
    素朴に再現する(退行検知用の参照実装)。"""
    prices = (
        session.query(PriceSnapshot.trade_date, PriceSnapshot.close)
        .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.trade_date <= as_of, PriceSnapshot.close.isnot(None))
        .order_by(PriceSnapshot.trade_date)
        .all()
    )
    series = (
        session.query(MacroSeries.observation_date, MacroSeries.value)
        .filter(MacroSeries.series_id == factor, MacroSeries.observation_date <= as_of, MacroSeries.value.isnot(None))
        .order_by(MacroSeries.observation_date)
        .all()
    )
    price_returns = _returns(_weekly_last(prices), difference=False)
    factor_returns = _returns(_weekly_last(series), difference=False)
    common = sorted(set(price_returns) & set(factor_returns))
    result = macro_exposure([price_returns[k] for k in common], [factor_returns[k] for k in common])
    return result["beta"]


def test_hoisted_query_produces_the_same_beta_as_the_naive_per_ticker_computation(seeded):
    """S-6:クエリをループの外へ括り出しても出力が変わらないこと。"""
    ticker_ids = seeded
    as_of = _WEEKS[-1] + datetime.timedelta(days=1)

    counts = collect_macro_exposure(symbols=_SYMBOLS, observed_at=datetime.datetime(2098, 3, 1, tzinfo=datetime.timezone.utc), minimum_weeks=4)
    assert counts["targets"] == len(_SYMBOLS)
    assert counts["with_data"] == len(_SYMBOLS)
    assert counts["failed"] == 0

    with session_scope() as session:
        for symbol, ticker_id in ticker_ids.items():
            rows = {
                row.factor: row.beta
                for row in session.query(MacroExposureSnapshot).filter_by(ticker_id=ticker_id).all()
            }
            assert set(rows) == set(_FACTORS)
            for factor in _FACTORS:
                expected = _naive_expected_beta(session, ticker_id, factor, as_of)
                assert expected is not None
                # `MacroExposureSnapshot.beta` は Numeric(12, 6) 列(小数点以下
                # 6桁)に保存されるため、絶対誤差でそのDB丸めぶんだけ許容する。
                assert float(rows[factor]) == pytest.approx(expected, abs=5e-7)


def test_factor_series_is_queried_once_per_factor_not_once_per_ticker(seeded):
    """退行検知:factor系列への問い合わせがticker数に比例しないこと。

    以前はticker(3件)×factor(2件)=6回のはずが、括り出す前は
    ticker毎に毎回フルスキャンし直していた。ここを再びticketループの中へ
    戻すと、この本数が3倍(tickers数倍)に増えて失敗する。
    """
    # `macro_series`にはこのDB(実運用のFRED収集分等)にある全distinct系列が
    # 対象になる仕様——`collect_macro_exposure`はfactorを絞り込まない。
    # そのため期待値は「このテストの2系列だけ」ではなく、実際にDBにある
    # distinct系列数から動的に求める(環境によって既存データが変わりうる
    # ため、決め打ちにしない)。
    with session_scope() as session:
        all_factors = [row[0] for row in session.query(MacroSeries.series_id).distinct().all()]
    assert set(_FACTORS) <= set(all_factors)

    query_count = 0

    def _count_macro_series_queries(conn, cursor, statement, parameters, context, executemany):
        nonlocal query_count
        if "macro_series" in statement.lower():
            query_count += 1

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _count_macro_series_queries)
    try:
        collect_macro_exposure(
            symbols=_SYMBOLS, observed_at=datetime.datetime(2098, 3, 1, tzinfo=datetime.timezone.utc), minimum_weeks=4
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count_macro_series_queries)

    # distinct系列一覧取得1回 + 系列ごとの全履歴取得(factor数ぶん)だけで、
    # ticker数(3件)には比例しないこと。以前はticker×factorの回数
    # (このテストなら3×len(all_factors))だけ発行されていた。
    expected = 1 + len(all_factors)
    assert query_count == expected, (
        f"macro_series へのクエリが {query_count} 回発行された(期待値: {expected}回 = "
        "factor一覧1回 + factor系列 distinct数ぶん。tickerループの中に系列取得が"
        "戻っていないか確認すること)"
    )
