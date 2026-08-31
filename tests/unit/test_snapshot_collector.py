import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autoscreener.collectors.snapshot_collector import _register_failure, collect_one
from autoscreener.config import (
    CircuitBreakerConfig,
    CollectionConfig,
    QuarantineConfig,
    RetryConfig,
)
from autoscreener.db.models import CollectionLog, PriceSnapshot, RawSnapshot, Ticker, TickerAlias
from autoscreener.db.session import session_scope


def _make_config(threshold: int) -> CollectionConfig:
    return CollectionConfig(
        max_workers=1,
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=1, failure_rate_threshold=0.5),
        quarantine=QuarantineConfig(consecutive_failure_threshold=threshold, retry_interval_days=7),
    )


def test_ticker_is_quarantined_after_threshold_consecutive_failures():
    ticker = SimpleNamespace(consecutive_failures=0, is_quarantined=False)
    config = _make_config(threshold=5)

    for _ in range(4):
        _register_failure(ticker, config)
    assert ticker.is_quarantined is False
    assert ticker.consecutive_failures == 4

    _register_failure(ticker, config)
    assert ticker.consecutive_failures == 5
    assert ticker.is_quarantined is True


def test_ticker_not_quarantined_below_threshold():
    ticker = SimpleNamespace(consecutive_failures=0, is_quarantined=False)
    config = _make_config(threshold=5)

    for _ in range(3):
        _register_failure(ticker, config)

    assert ticker.is_quarantined is False


# --- delisted_atのクリア(24.7で発見。設定はされるがどこからもクリアされず、
# 誤判定や14.5のティッカー再利用ケースで永久にゲート対象外になっていた) ------


def _cleanup(symbol: str) -> None:
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if ticker is not None:
            session.query(CollectionLog).filter_by(ticker_id=ticker.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


def _financials_payload() -> dict:
    return {
        "info": {"marketCap": 1_000_000_000, "sector": "Technology", "currency": "USD"},
        "quarterly_income_stmt": {},
        "income_stmt": {},
        "balance_sheet": {},
        "cash_flow": {},
        "quarterly_cash_flow": {},
        "eps_revisions": {},
        "earnings_dates": [],
        "insider_transactions": [],
    }


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_successful_collection_clears_stale_delisted_at(mock_fetch_financials, mock_fetch_price, mock_fetch_isin):
    symbol = "ZZDELIST1"
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US", delisted_at=datetime.datetime.now(datetime.UTC))
            session.add(ticker)
            session.flush()
            assert ticker.delisted_at is not None

            config = _make_config(threshold=5)
            status = collect_one(session, uuid.uuid4(), symbol, config, datetime.date.today())

            assert status == "success"
            assert ticker.delisted_at is None
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_persistent_empty_response_is_eventually_treated_as_delisted(mock_fetch_financials):
    """B-5(docs/model_audit_v4_2026-08-26.md): yfinanceはHTTP 404を例外にせず空の
    `info`として返すことがあり、その場合は`EmptyResponseError`(=PermanentFailure
    ではない)になるため`delisted_at`が一度も設定されず、バックテストの
    生存バイアスが恒久化していた。連続失敗が閾値を超えたら`delisted_at`を
    設定して回復させる。
    """
    from autoscreener.collectors.errors import EmptyResponseError

    symbol = "ZZEMPTYRESP1"
    _cleanup(symbol)
    try:
        mock_fetch_financials.side_effect = EmptyResponseError(f"{symbol}: info missing all required fields")

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()

            config = _make_config(threshold=5)
            config = config.model_copy(
                update={"quarantine": config.quarantine.model_copy(update={"empty_response_delisted_threshold": 3})}
            )

            for _ in range(2):
                status = collect_one(session, uuid.uuid4(), symbol, config, datetime.date.today())
                assert status == "empty_response"
                assert ticker.delisted_at is None

            status = collect_one(session, uuid.uuid4(), symbol, config, datetime.date.today())
            assert status == "empty_response_delisted"
            assert ticker.delisted_at is not None
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value="US1111111111")
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_isin_mismatch_on_recovery_creates_new_ticker_and_archives_old(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    # 14.5:廃止された銘柄が同じシンボルのまま復活したように見えても、ISINが
    # 一致しなければ「別会社によるシンボル再利用」であり、同じticker_idに
    # データを混入させてはいけない。
    symbol = "ZZREUSE1"
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            old_ticker = Ticker(
                symbol=symbol,
                market="US",
                isin="US0000000000",
                delisted_at=datetime.datetime.now(datetime.UTC),
            )
            session.add(old_ticker)
            session.flush()
            old_id = old_ticker.id

            config = _make_config(threshold=5)
            status = collect_one(session, uuid.uuid4(), symbol, config, datetime.date.today())
            session.flush()

            assert status == "success"

            archived = session.get(Ticker, old_id)
            assert archived is not None
            assert archived.symbol == f"{symbol}~D{old_id}"
            assert archived.delisted_at is not None  # 旧レコードは廃止状態のまま凍結される

            new_ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            assert new_ticker.id != old_id
            assert new_ticker.isin == "US1111111111"
            assert new_ticker.delisted_at is None

            alias = session.query(TickerAlias).filter_by(ticker_id=old_id, symbol=symbol).one()
            assert alias.effective_to == datetime.date.today()

            # 新しいtickerに対して収集結果(RawSnapshot)が紐づいており、旧レコードの
            # 履歴とは混ざっていないこと
            snapshot = session.query(RawSnapshot).filter_by(ticker_id=new_ticker.id).one()
            assert snapshot is not None
    finally:
        with session_scope() as session:
            ticker_ids = [
                row[0]
                for row in session.query(Ticker.id).filter(
                    (Ticker.symbol == symbol) | (Ticker.symbol.like(f"{symbol}~D%"))
                )
            ]
            for tid in ticker_ids:
                session.query(CollectionLog).filter_by(ticker_id=tid).delete()
                session.query(RawSnapshot).filter_by(ticker_id=tid).delete()
                session.query(TickerAlias).filter_by(ticker_id=tid).delete()
            session.query(Ticker).filter(Ticker.id.in_(ticker_ids)).delete(synchronize_session=False)


# --- 13.4:日次経路で蓄積した価格履歴の分割遡及調整 ---------------------------


def _price_row_values(session, ticker_id: int) -> dict:
    return {
        row.trade_date: (float(row.close), row.volume, row.shares_outstanding)
        for row in session.query(PriceSnapshot).filter_by(ticker_id=ticker_id).all()
    }


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price")
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_split_retroactively_adjusts_stored_price_history(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """3:1分割が起きた日、蓄積済みの分割前の行が分割後の単位に揃うこと。
    揃えないと12-1モメンタムが「−67%の暴落」を、希薄化CAGRが「株式数3倍」を
    誤検知する(13.4のバックフィル側の罠が日次経路にだけ残っていた)。"""
    symbol = "ZZSPLIT1"
    split_date = datetime.date(2026, 6, 15)
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = _financials_payload()
        mock_fetch_price.return_value = {
            "trade_date": split_date,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 3_000,
            "shares_outstanding": 3_000_000,
            "recent_splits": [(split_date, 3.0)],
        }

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            for offset in (2, 1):
                session.add(
                    PriceSnapshot(
                        ticker_id=ticker.id,
                        trade_date=split_date - datetime.timedelta(days=offset),
                        open=300.0,
                        high=310.0,
                        low=295.0,
                        close=300.0,
                        volume=1_000,
                        shares_outstanding=1_000_000,
                    )
                )
            session.flush()

            config = _make_config(threshold=5)
            assert collect_one(session, uuid.uuid4(), symbol, config, split_date) == "success"
            session.flush()

            values = _price_row_values(session, ticker.id)
            for offset in (2, 1):
                close, volume, shares = values[split_date - datetime.timedelta(days=offset)]
                assert round(close, 4) == 100.0
                assert volume == 3_000
                assert shares == 3_000_000
            assert values[split_date][0] == 100.0

            # べき等性(18.3):同じ日に再実行しても二重に割らない
            assert collect_one(session, uuid.uuid4(), symbol, config, split_date) == "success"
            session.flush()
            assert _price_row_values(session, ticker.id) == values
    finally:
        _cleanup(symbol)


# --- 14.10 前日比急変検知の配線(2026-08-26に発見:実装済みだが未呼び出しだった) ---


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_day_over_day_spike_is_recorded_without_blocking_collection(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """`detect_day_over_day_spike` は実装もテストもされていたが、**どこからも
    呼ばれていなかった**(`min_listed_quarters`・`is_valid`・`available_from` に
    続く4例目の「実装済みだが未配線」)。

    検知しても収集は止めない・`is_valid` も動かさない——急変は「異常の疑い」で
    あって「値が間違っている証明」ではないため。`collection_logs.detail` に
    残して運用者が気づける状態にするところまでが仕様(14.10)。
    """
    symbol = "ZZSPIKE1"
    _cleanup(symbol)
    try:
        config = _make_config(threshold=5)
        run_id = uuid.uuid4()

        payload = _financials_payload()
        payload["info"]["totalRevenue"] = 100_000_000
        mock_fetch_financials.return_value = payload

        with session_scope() as session:
            session.add(Ticker(symbol=symbol, market="US"))
            session.flush()
            assert collect_one(session, run_id, symbol, config, datetime.date(2099, 3, 1)) == "success"

        # 翌日、売上が100倍になって返ってきた(単位変更・yfinance側の不具合の典型)
        spiked = _financials_payload()
        spiked["info"]["totalRevenue"] = 10_000_000_000
        mock_fetch_financials.return_value = spiked

        with session_scope() as session:
            assert collect_one(session, run_id, symbol, config, datetime.date(2099, 3, 2)) == "success"

        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            logs = (
                session.query(CollectionLog)
                .filter_by(ticker_id=ticker.id)
                .order_by(CollectionLog.snapshot_date.asc())
                .all()
            )
            assert (logs[0].detail or {}).get("day_over_day_spikes") is None
            assert "totalRevenue_day_over_day_spike" in logs[1].detail["day_over_day_spikes"]
            # 収集自体は成功扱いのまま(ゲート・スコアには影響させない)
            assert logs[1].status == "success"
    finally:
        _cleanup(symbol)


# --- 13.4 分割の遡及調整(2026-08-26:ガードが行の有無だけで判断していた) ---


def _seed_prices(session, ticker_id, days: dict[datetime.date, float]) -> None:
    for day, close in days.items():
        session.add(
            PriceSnapshot(
                ticker_id=ticker_id, trade_date=day, open=close, high=close,
                low=close, close=close, volume=1_000, shares_outstanding=1_000_000,
            )
        )


def test_split_is_applied_even_when_a_post_split_row_already_exists():
    """分割当日の収集がYahooの分割反映より先に走ると、分割前価格の行が
    「分割日以降の行」として残る。旧ガード(`分割日以降の行があるならスキップ`)は
    そこで永久に調整を止めてしまい、価格・株式数の単位が混ざったまま固定される。

    実データで FLOC がこの状態だった(2026年5月の株式併合が反映されず、
    発行済株式数が 22M → 93M → 42M と単位の混ざった系列になっていた)。
    保存値と取得値(常に分割調整済み)を同じ取引日で比べれば、行の有無ではなく
    値そのもので判定できる。
    """
    from autoscreener.collectors.snapshot_collector import _reconcile_splits

    symbol = "ZZSPLIT1"
    _cleanup(symbol)
    try:
        split_date = datetime.date(2099, 5, 20)
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            # 1:4の株式併合(yfinanceの "Stock Splits" は 0.25 で返る)。
            # 保存済みは**併合前の単位**なので株価は1/4の水準。
            _seed_prices(session, ticker.id, {
                datetime.date(2099, 5, 18): 2.5,
                datetime.date(2099, 5, 19): 2.5,
                split_date: 2.5,          # ← 分割当日の行が既にある(旧ガードが誤作動する条件)
            })
            session.flush()

            # 取得しなおした終値は常に現在単位(併合後なので4倍)
            recent_closes = {
                datetime.date(2099, 5, 18): 10.0,
                datetime.date(2099, 5, 19): 10.0,
            }
            _reconcile_splits(session, ticker.id, [(split_date, 0.25)], recent_closes)
            session.flush()

            rows = {
                r.trade_date: float(r.close)
                for r in session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).all()
            }
            # 併合前の行が現在単位へ引き直される(2.5 / 0.25 = 10.0)
            assert rows[datetime.date(2099, 5, 18)] == pytest.approx(10.0)
            assert rows[datetime.date(2099, 5, 19)] == pytest.approx(10.0)
            # 分割日以降の行は触らない
            assert rows[split_date] == pytest.approx(2.5)
    finally:
        _cleanup(symbol)


def test_split_is_not_applied_twice_when_stored_rows_already_match():
    """保存値と取得値が一致していれば調整済み。再実行しても二重適用しない(18.3)。"""
    from autoscreener.collectors.snapshot_collector import _reconcile_splits

    symbol = "ZZSPLIT2"
    _cleanup(symbol)
    try:
        split_date = datetime.date(2099, 5, 20)
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            _seed_prices(session, ticker.id, {datetime.date(2099, 5, 19): 10.0})
            session.flush()

            _reconcile_splits(
                session, ticker.id, [(split_date, 0.25)], {datetime.date(2099, 5, 19): 10.0}
            )
            session.flush()
            row = session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).one()
            assert float(row.close) == pytest.approx(10.0)
    finally:
        _cleanup(symbol)
