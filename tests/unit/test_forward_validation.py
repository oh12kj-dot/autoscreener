"""前方検証ジョブのテスト。test_api_routes.pyと同様、ローカル開発用DBに対して
専用ティッカーを作成・削除する形で実行する。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.db.models import ForwardReturn, PriceSnapshot, RawSnapshot, Score, Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope
from autoscreener.scoring.forward_validation import run_forward_validation


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
        for t in tickers:
            session.query(ForwardReturn).filter_by(ticker_id=t.id).delete()
            session.query(Score).filter_by(ticker_id=t.id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=t.id).delete()
            session.query(UniverseSnapshot).filter_by(ticker_id=t.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=t.id).delete()
            session.delete(t)


@pytest.fixture
def matured_ticker():
    """スコア確定から40日経過(1Mホライズンは満期、3M以降は未満期)している銘柄。"""
    symbol = "ZZFWD1"
    _cleanup([symbol])
    score_date = datetime.date(2020, 1, 1)
    entry_date = datetime.date(2020, 1, 2)
    exit_date_1m = datetime.date(2020, 1, 31)  # score_date + 30日 = 2020-01-31

    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(
            Score(
                ticker_id=ticker_id,
                score_date=score_date,
                scoring_version="v1",
                config_hash="test",
                probability=0.0070,
            )
        )
        session.add(PriceSnapshot(ticker_id=ticker_id, trade_date=entry_date, open=100.0, close=101.0, volume=1000))
        session.add(PriceSnapshot(ticker_id=ticker_id, trade_date=exit_date_1m, open=119.0, close=120.0, volume=1000))

    yield symbol, ticker_id
    _cleanup([symbol])


def _get_ticker_id(symbol: str) -> int:
    with session_scope() as session:
        return session.query(Ticker.id).filter_by(symbol=symbol).scalar()


def test_matured_horizon_computes_realized_return(matured_ticker):
    symbol, _ = matured_ticker
    as_of = datetime.date(2020, 2, 5)  # score_date+30日(1M)は満期、+91日(3M)はまだ

    result = run_forward_validation(as_of_date=as_of)

    assert result["computed"] >= 1
    # 熟した行がある実行でも、境界情報のキー自体は常に返る(2026-09-05)。
    assert isinstance(result["too_recent"], int)
    assert result["cutoff_date"] == (as_of - datetime.timedelta(days=30)).isoformat()

    ticker_id = _get_ticker_id(symbol)
    with session_scope() as session:
        fr = (
            session.query(ForwardReturn)
            .filter_by(ticker_id=ticker_id, base_date=datetime.date(2020, 1, 1), horizon="1M")
            .one_or_none()
        )
        assert fr is not None
        # entry=100.0(翌営業日始値), exit=120.0(1M後以降で最初に観測できる終値)
        assert round(float(fr.realized_return), 4) == round(120.0 / 100.0 - 1, 4)

        fr_3m = (
            session.query(ForwardReturn)
            .filter_by(ticker_id=ticker_id, base_date=datetime.date(2020, 1, 1), horizon="3M")
            .one_or_none()
        )
        assert fr_3m is None  # 3Mはまだ満期していないので記録されない


def test_not_matured_before_shortest_horizon_is_skipped_entirely():
    symbol = "ZZFWD2"
    _cleanup([symbol])
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            session.add(
                Score(
                    ticker_id=ticker.id,
                    score_date=datetime.date(2026, 8, 20),
                    scoring_version="v1",
                    config_hash="test",
                    probability=0.0070,
                )
            )
            session.add(
                PriceSnapshot(ticker_id=ticker.id, trade_date=datetime.date(2026, 8, 21), open=50.0, close=51.0, volume=100)
            )

        # スコア確定からわずか5日後 -> 最短ホライズン(1M=30日)にも満たない
        as_of = datetime.date(2026, 8, 25)
        result = run_forward_validation(as_of_date=as_of)

        ticker_id = _get_ticker_id(symbol)
        with session_scope() as session:
            count = session.query(ForwardReturn).filter_by(ticker_id=ticker_id).count()
            assert count == 0

        # 観測可能性の境界情報(2026-09-05):全カウンタ0が「何も熟していない」
        # 正常状態であることを、ソースを読まずに区別できる材料が結果に無ければ
        # ならない。cutoffは as_of_date - 30日 から一意に決まるので厳密比較できる。
        assert result["computed"] == 0
        expected_cutoff = as_of - datetime.timedelta(days=30)
        assert result["cutoff_date"] == expected_cutoff.isoformat()
        # このテストの行(score_date=2026-08-20)自体がcutoffより後なので、
        # 少なくとも1件はtoo_recentとして数えられているはず。
        assert result["too_recent"] >= 1
        # oldest_score_date/first_horizon_matures_onは全テーブル横断のMINなので
        # 共有テストDB上の値そのものは固定できないが、返ってきた2つの値は
        # 「最古スコア日 + 最短ホライズン(30日)」という関係を必ず満たす。
        assert result["oldest_score_date"] is not None
        assert result["first_horizon_matures_on"] is not None
        oldest = datetime.date.fromisoformat(result["oldest_score_date"])
        matures = datetime.date.fromisoformat(result["first_horizon_matures_on"])
        assert matures == oldest + datetime.timedelta(days=30)
        # 我々が挿入した行より古いスコアがテーブル全体のどこかにあるとは限らないが、
        # 少なくとも我々の行より新しいということは無い(MINである以上)。
        assert oldest <= datetime.date(2026, 8, 20)
    finally:
        _cleanup([symbol])


def test_missing_exit_price_is_not_computed():
    symbol = "ZZFWD3"
    _cleanup([symbol])
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            session.add(
                Score(
                    ticker_id=ticker.id,
                    score_date=datetime.date(2020, 1, 1),
                    scoring_version="v1",
                    config_hash="test",
                    probability=0.0070,
                )
            )
            # エントリー価格のみ存在し、1M後の価格データが無い
            session.add(
                PriceSnapshot(ticker_id=ticker.id, trade_date=datetime.date(2020, 1, 2), open=100.0, close=101.0, volume=100)
            )

        result = run_forward_validation(as_of_date=datetime.date(2020, 2, 5))
        assert result["missing_price"] >= 1

        ticker_id = _get_ticker_id(symbol)
        with session_scope() as session:
            count = session.query(ForwardReturn).filter_by(ticker_id=ticker_id).count()
            assert count == 0
    finally:
        _cleanup([symbol])


def test_rerun_does_not_recompute_existing_result(matured_ticker):
    symbol, _ = matured_ticker
    as_of = datetime.date(2020, 2, 5)

    run_forward_validation(as_of_date=as_of)
    result_second_run = run_forward_validation(as_of_date=as_of)

    # 1M分はすでに算出済みのため、2回目の実行では新規計算されない
    ticker_id = _get_ticker_id(symbol)
    with session_scope() as session:
        count = session.query(ForwardReturn).filter_by(ticker_id=ticker_id, horizon="1M").count()
        assert count == 1  # 重複行が作られていない


# ============================================================================
# 27.11:上場廃止の決済(生存バイアスの修正)
# ============================================================================


@pytest.fixture
def delisted_ticker():
    """スコア確定後に取引が途切れ、`delisted_at` が立っている銘柄。

    修正前は、この銘柄の実現リターンが**1件も記録されなかった**。上場廃止は
    −90%〜−100%という最悪の結果と強く相関するため、検証データから負けの
    極端値だけが系統的に消え、14.2のKPIが実態より必ず良く出ていた。
    """
    symbol = "ZZDELIST"
    _cleanup([symbol])
    with session_scope() as session:
        ticker = Ticker(
            symbol=symbol,
            market="US",
            delisted_at=datetime.datetime(2020, 2, 1, tzinfo=datetime.UTC),
        )
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(
            Score(
                ticker_id=ticker_id,
                score_date=datetime.date(2020, 1, 1),
                scoring_version="v1",
                config_hash="test",
                probability=0.05,
            )
        )
        # 建玉は取れる
        session.add(
            PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2020, 1, 2), open=100.0, close=100.0)
        )
        # 目標日(2020-01-31)より前に取引が途切れ、最終価格は8ドルまで暴落
        session.add(
            PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2020, 1, 20), open=9.0, close=8.0)
        )
    yield symbol, ticker_id
    _cleanup([symbol])


def test_delisted_ticker_is_settled_at_its_last_observed_price(delisted_ticker):
    """負けの極端値が検証資産から消えないこと(27.11)。"""
    _symbol, ticker_id = delisted_ticker
    result = run_forward_validation(as_of_date=datetime.date(2020, 2, 15))
    assert result["settled_delisted"] >= 1

    with session_scope() as session:
        row = (
            session.query(ForwardReturn)
            .filter_by(ticker_id=ticker_id, base_date=datetime.date(2020, 1, 1), horizon="1M")
            .one()
        )
        assert row.settlement == "delisted"
        # 100ドルで建てて最終観測8ドル = −92%
        assert float(row.realized_return) == pytest.approx(-0.92, abs=1e-6)


def test_delisting_uses_last_price_so_buyouts_are_not_counted_as_total_losses():
    """上場廃止の原因は破綻だけではない。買収(TOB)では最終価格まで上がる。

    −100%を決め打ちすると、買収で終わった銘柄を全損として記録してしまい、
    今度は逆向きにKPIを歪めることになる。
    """
    symbol = "ZZBUYOUT"
    _cleanup([symbol])
    try:
        with session_scope() as session:
            ticker = Ticker(
                symbol=symbol,
                market="US",
                delisted_at=datetime.datetime(2020, 2, 1, tzinfo=datetime.UTC),
            )
            session.add(ticker)
            session.flush()
            ticker_id = ticker.id
            session.add(
                Score(
                    ticker_id=ticker_id,
                    score_date=datetime.date(2020, 1, 1),
                    scoring_version="v1",
                    config_hash="test",
                    probability=0.05,
                )
            )
            session.add(
                PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2020, 1, 2), open=100.0, close=100.0)
            )
            # 買収価格150ドルで取引終了
            session.add(
                PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2020, 1, 20), open=150.0, close=150.0)
            )

        run_forward_validation(as_of_date=datetime.date(2020, 2, 15))
        with session_scope() as session:
            row = (
                session.query(ForwardReturn)
                .filter_by(ticker_id=ticker_id, base_date=datetime.date(2020, 1, 1), horizon="1M")
                .one()
            )
            assert row.settlement == "delisted"
            assert float(row.realized_return) == pytest.approx(0.50, abs=1e-6)
    finally:
        _cleanup([symbol])


def test_live_ticker_with_a_data_gap_is_not_settled_as_delisted():
    """上場は続いているのに価格が欠測しているだけの銘柄を、損失として確定しない。

    確定してしまうと、収集の失敗が「その銘柄が下落した」という記録に化ける。
    未確定のまま次回実行に委ねるのが正しい。
    """
    symbol = "ZZGAP"
    _cleanup([symbol])
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")  # delisted_at なし・隔離なし
            session.add(ticker)
            session.flush()
            ticker_id = ticker.id
            session.add(
                Score(
                    ticker_id=ticker_id,
                    score_date=datetime.date(2020, 1, 1),
                    scoring_version="v1",
                    config_hash="test",
                    probability=0.05,
                )
            )
            session.add(
                PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2020, 1, 2), open=100.0, close=100.0)
            )

        result = run_forward_validation(as_of_date=datetime.date(2020, 2, 15))
        assert result["missing_price"] >= 1
        with session_scope() as session:
            assert (
                session.query(ForwardReturn)
                .filter_by(ticker_id=ticker_id, base_date=datetime.date(2020, 1, 1), horizon="1M")
                .one_or_none()
                is None
            )
    finally:
        _cleanup([symbol])


def test_seven_year_horizon_exists():
    """14.1の「10バガーの評価ホライズンは7年」が実際に計上されること。"""
    from autoscreener.scoring.forward_validation import HORIZONS

    assert ("7Y", 2557) in HORIZONS
