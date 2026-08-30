"""週次ユニバース更新のテスト(B-8、model_audit_v4_2026-08-26.md)。"""

import datetime
from unittest.mock import patch

from autoscreener.batch.universe_refresh import refresh_universe
from autoscreener.collectors.universe_source import CandidateTicker
from autoscreener.db.models import Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        for symbol in symbols:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker is not None:
                session.query(UniverseSnapshot).filter_by(ticker_id=ticker.id).delete()
                session.delete(ticker)


@patch("autoscreener.batch.universe_refresh._RATIO_GUARD_MIN_TRACKED", float("inf"))
@patch("autoscreener.batch.universe_refresh.fetch_universe_candidates")
def test_tickers_missing_from_the_candidate_list_are_quarantined(mock_fetch):
    """B-8: フィルタ実装前に登録された残骸(優先株等)が、候補リストから
    消えた次の週次更新で隔離され、日次収集の対象から外れること。

    **`_RATIO_GUARD_MIN_TRACKED` を無効化している。** `refresh_universe` の
    掃引比率ガードは `market == "US" かつ is_quarantined=False` の**全ティッカー**
    を分母にする(symbolで絞り込まない)。開発DBには本番相当のティッカーが
    数千件あり、このテストが作る候補は1件だけなので、ガードを有効なままにすると
    「追跡中の97%超を隔離しようとしている」と判定されて掃引ごと見送られ、
    本テストが検証したい「候補から消えた銘柄は隔離される」という本筋の挙動が
    確認できなくなる(実際に2026-08-30、開発DBの実データで発生した)。
    ガードそのものの動作は下の `test_sweep_is_skipped_when_it_would_quarantine_too_many`
    が(逆方向に、値を`1`にして)別途検証している。
    """
    stale_symbol = "ZZSTALEPREF$D"
    fresh_symbol = "ZZFRESHCOMMON1"
    _cleanup([stale_symbol, fresh_symbol])
    try:
        with session_scope() as session:
            session.add(Ticker(symbol=stale_symbol, market="US", is_quarantined=False))
            session.flush()

        mock_fetch.return_value = [
            CandidateTicker(symbol=fresh_symbol, security_name="Fresh Co", exchange="NASDAQ", is_etf=False, is_test_issue=False)
        ]

        refresh_universe(snapshot_date=datetime.date.today())

        with session_scope() as session:
            stale = session.query(Ticker).filter_by(symbol=stale_symbol).one()
            fresh = session.query(Ticker).filter_by(symbol=fresh_symbol).one()
            assert stale.is_quarantined is True
            assert fresh.is_quarantined is False
    finally:
        _cleanup([stale_symbol, fresh_symbol])


@patch("autoscreener.batch.universe_refresh.fetch_universe_candidates")
def test_tickers_are_not_deleted_only_quarantined(mock_fetch):
    """B-8: 削除はしない(forward_returns・backtest_runsが参照している可能性)。"""
    stale_symbol = "ZZSTALEPREF2$D"
    _cleanup([stale_symbol])
    try:
        with session_scope() as session:
            session.add(Ticker(symbol=stale_symbol, market="US", is_quarantined=False))
            session.flush()

        mock_fetch.return_value = []
        refresh_universe(snapshot_date=datetime.date.today())

        with session_scope() as session:
            # 候補が空(取得失敗)のときは何も隔離しない安全側の挙動
            still_there = session.query(Ticker).filter_by(symbol=stale_symbol).one_or_none()
            assert still_there is not None
            assert still_there.is_quarantined is False
    finally:
        _cleanup([stale_symbol])


@patch("autoscreener.batch.universe_refresh._RATIO_GUARD_MIN_TRACKED", 1)
@patch("autoscreener.batch.universe_refresh.fetch_universe_candidates")
def test_sweep_is_skipped_when_it_would_quarantine_too_many(mock_fetch):
    """B-8: 候補リストの取得が部分的に失敗した場合(例外にならず件数だけ減る)、
    掃引をそのまま実行するとユニバースを大量に誤隔離し、次の週次更新まで誰も
    気づけない。隔離しようとする件数が追跡中の銘柄に対して大きすぎるときは
    掃引ごと見送ること。"""
    stale_symbol = "ZZTRUNCATED1"
    fresh_symbol = "ZZTRUNCCAND1"
    _cleanup([stale_symbol, fresh_symbol])
    try:
        with session_scope() as session:
            session.add(Ticker(symbol=stale_symbol, market="US", is_quarantined=False))
            session.flush()

        # 候補に既存銘柄が1つも含まれない = 追跡中の100%を隔離しようとする状況
        mock_fetch.return_value = [
            CandidateTicker(
                symbol=fresh_symbol, security_name="Truncated Co", exchange="NASDAQ",
                is_etf=False, is_test_issue=False,
            )
        ]
        refresh_universe(snapshot_date=datetime.date.today())

        with session_scope() as session:
            stale = session.query(Ticker).filter_by(symbol=stale_symbol).one()
            assert stale.is_quarantined is False, "部分取得の疑いがあるときは隔離してはいけない"
    finally:
        _cleanup([stale_symbol, fresh_symbol])
