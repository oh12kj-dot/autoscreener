import datetime
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autoscreener.batch.apply_gates import _gather_gate_input
from autoscreener.collectors.snapshot_collector import _register_failure, collect_one
from autoscreener.config import (
    CircuitBreakerConfig,
    CollectionConfig,
    QuarantineConfig,
    RetryConfig,
)
from autoscreener.db.models import CollectionLog, EventCalendar, Filing, PriceSnapshot, RawSnapshot, Ticker, TickerAlias
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
            session.query(EventCalendar).filter_by(ticker_id=ticker.id).delete()
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
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


# --- S-2(docs/daily_pipeline_throughput_plan_2026-09-04.md):財務諸表の週次化 + 持ち越し ---
#
# `fetch_raw_financials`は`include_statements=False`のとき財務諸表5キー
# (quarterly_income_stmt・income_stmt・balance_sheet・cash_flow・
# quarterly_cash_flow)を返さない。`collect_one`は非週次日かつ持ち越し元
# (直近raw_snapshot)がある銘柄にだけこれを渡し、欠けたキーを直近payloadから
# 補う——payloadの形(キー集合)を日によって変えないことで、`apply_gates.py`
# 等の「最新1件」読み取りが財務諸表を欠いた行に当たらないようにする。

_MONDAY = datetime.date(2099, 3, 2)  # 週次日(WEEKLY_REFRESH_WEEKDAY=0)
_TUESDAY = datetime.date(2099, 3, 3)  # 非週次日


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_new_ticker_fetches_statements_even_on_a_non_weekly_day(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """持ち越し元(直近raw_snapshot)が無い銘柄は、週次日でなくても財務諸表を
    取得する(新規上場・初回収集がここに当たる)。"""
    symbol = "ZZWEEKLY1"
    _cleanup(symbol)
    try:
        assert _TUESDAY.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            session.add(Ticker(symbol=symbol, market="US"))
            session.flush()
            config = _make_config(threshold=5)
            assert collect_one(session, uuid.uuid4(), symbol, config, _TUESDAY) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_monday_always_fetches_statements(mock_fetch_financials, mock_fetch_price, mock_fetch_isin):
    """週次日(月曜)は持ち越し元があっても財務諸表を取得する。"""
    symbol = "ZZWEEKLY3"
    _cleanup(symbol)
    try:
        assert _MONDAY.weekday() == 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            # 直近raw_snapshotが既にある状態(持ち越し元はあるが月曜なので使わない)
            session.add(RawSnapshot(
                ticker_id=ticker.id, snapshot_date=_MONDAY - datetime.timedelta(days=7),
                source="yfinance", payload=_financials_payload(), content_hash="zzweekly3-prev",
                last_seen_date=_MONDAY - datetime.timedelta(days=7),
                available_from=_MONDAY - datetime.timedelta(days=7),
            ))
            config = _make_config(threshold=5)
            assert collect_one(session, uuid.uuid4(), symbol, config, _MONDAY) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_non_weekly_day_carries_forward_statements_and_gates_still_find_them(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """S-2の本丸:財務諸表を取得しない日でも、保存されるpayloadは直近の
    財務諸表を持ち越し、キー集合を変えない。さらに`apply_gates._gather_gate_input`
    が、その持ち越された財務諸表を最新1件のpayloadから正しく読めること
    (gatesが静かに壊れていないことの確認)。
    """
    symbol = "ZZWEEKLY2"
    _cleanup(symbol)
    try:
        full_payload = _financials_payload()
        full_payload["balance_sheet"] = {"Stockholders Equity": {"2098-12-31": 500.0}}
        full_payload["quarterly_income_stmt"] = {
            "Total Revenue": {f"2098-{m:02d}-30": 1.0e7 for m in (3, 6, 9, 12)}
        }
        mock_fetch_financials.return_value = full_payload

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            config = _make_config(threshold=5)
            assert collect_one(session, uuid.uuid4(), symbol, config, _MONDAY) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True

        # 火曜日はinfoだけ返す(実際のfetch_raw_financials(include_statements=
        # False)の挙動を模倣——財務諸表5キーを一切含まない)。
        info_only = {"info": {"marketCap": 1_100_000_000, "sector": "Technology", "currency": "USD"}}
        mock_fetch_financials.return_value = info_only

        with session_scope() as session:
            assert collect_one(session, uuid.uuid4(), symbol, config, _TUESDAY) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is False

        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            latest = (
                session.query(RawSnapshot)
                .filter_by(ticker_id=ticker.id)
                .order_by(RawSnapshot.snapshot_date.desc())
                .first()
            )
            assert latest.snapshot_date == _TUESDAY
            # payloadの形(キー集合)が変わっていないこと
            assert set(latest.payload.keys()) >= {
                "info", "quarterly_income_stmt", "income_stmt",
                "balance_sheet", "cash_flow", "quarterly_cash_flow",
            }
            # 月曜日に取得した財務諸表がそのまま持ち越されていること
            assert latest.payload["balance_sheet"] == {"Stockholders Equity": {"2098-12-31": 500.0}}
            # infoは火曜日に取得した新しい値
            assert latest.payload["info"]["marketCap"] == 1_100_000_000

            # gatesが持ち越された財務諸表を最新1件のpayloadから読めること。
            # (価格が無いのでmedian_daily_dollar_volume等はNoneのままでよい
            # ——ここで確認したいのは財務諸表由来の値だけ)
            gate_input = _gather_gate_input(session, ticker, _TUESDAY)
            assert gate_input is not None
            assert gate_input.stockholders_equity == pytest.approx(500.0)
            assert gate_input.available_quarters == 4
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_recovering_from_delisted_always_fetches_statements(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """delisted_atが設定されている銘柄は、非週次日でも常に財務諸表を取得する
    (ISIN不一致による別ticker_idへの再割当て(14.5)が起こりうるため、
    持ち越し判定の対象から外す)。"""
    symbol = "ZZWEEKLY4"
    _cleanup(symbol)
    try:
        assert _TUESDAY.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()
        mock_fetch_isin.return_value = None  # ISIN未設定同士なので一致扱いのまま復旧する

        with session_scope() as session:
            ticker = Ticker(
                symbol=symbol, market="US", delisted_at=datetime.datetime.now(datetime.UTC)
            )
            session.add(ticker)
            session.flush()
            session.add(RawSnapshot(
                ticker_id=ticker.id, snapshot_date=_TUESDAY - datetime.timedelta(days=1),
                source="yfinance", payload=_financials_payload(), content_hash="zzweekly4-prev",
                last_seen_date=_TUESDAY - datetime.timedelta(days=1),
                available_from=_TUESDAY - datetime.timedelta(days=1),
            ))
            config = _make_config(threshold=5)
            assert collect_one(session, uuid.uuid4(), symbol, config, _TUESDAY) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_carry_forward_does_not_create_a_new_row_when_nothing_actually_changed(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """S-2実装の副作用チェック:持ち越しによって`payload`の中身自体は
    (infoも含めて)前日と完全に同一なら、14.11の内容ハッシュ判定どおり
    新規行を作らず`last_seen_date`だけ更新すること——`payload`を毎回作り直す
    実装(dictをコピーして持ち越す等)によって、意図せず「常に新規」または
    「常に重複」のどちらかに倒れていないかを確認する。
    """
    symbol = "ZZWEEKLY5"
    wednesday = datetime.date(2099, 3, 4)
    _cleanup(symbol)
    try:
        assert wednesday.weekday() != 0
        # `_financials_payload()`は27.16で廃止済みの旧キー(eps_revisions等)も
        # 含む固定フィクスチャなので、ここでは実際の`fetch_raw_financials`が
        # 返すキー集合(info + 財務諸表5キー)だけの payload を組み立てる
        # ——そうしないと日をまたいでも変わらないはずの部分に無関係な差分が
        # 生まれ、このテストの意図(中身が同一なら新規行を作らない)を
        # 検証できない。
        full_payload = {
            "info": {"marketCap": 1_000_000_000, "sector": "Technology", "currency": "USD"},
            "quarterly_income_stmt": {},
            "income_stmt": {},
            "balance_sheet": {"Stockholders Equity": {"2098-12-31": 500.0}},
            "cash_flow": {},
            "quarterly_cash_flow": {},
        }
        mock_fetch_financials.return_value = full_payload

        with session_scope() as session:
            session.add(Ticker(symbol=symbol, market="US"))
            session.flush()
            config = _make_config(threshold=5)
            # 1日目:新規銘柄なので財務諸表つきで丸ごと取得・保存される
            assert collect_one(session, uuid.uuid4(), symbol, config, _MONDAY) == "success"

        # 2日目(非週次日):infoも含めて前日と完全に同じ値を返す
        mock_fetch_financials.return_value = {"info": dict(full_payload["info"])}

        with session_scope() as session:
            assert collect_one(session, uuid.uuid4(), symbol, config, wednesday) == "success"

        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            rows = session.query(RawSnapshot).filter_by(ticker_id=ticker.id).all()
            # 内容(info含む)が変わっていないので新規行は作らない(14.11)
            assert len(rows) == 1
            assert rows[0].last_seen_date == wednesday
            assert rows[0].snapshot_date == _MONDAY

        # 3日目(非週次日):infoが変わった(実運用の通常ケース)→新規行を作る
        changed_info = dict(full_payload["info"])
        changed_info["marketCap"] = 999_000_000
        mock_fetch_financials.return_value = {"info": changed_info}
        thursday = datetime.date(2099, 3, 5)

        with session_scope() as session:
            assert collect_one(session, uuid.uuid4(), symbol, config, thursday) == "success"

        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            rows = (
                session.query(RawSnapshot)
                .filter_by(ticker_id=ticker.id)
                .order_by(RawSnapshot.snapshot_date.asc())
                .all()
            )
            assert len(rows) == 2
            assert rows[-1].snapshot_date == thursday
            # 持ち越された財務諸表は変わらず引き継がれている
            assert rows[-1].payload["balance_sheet"] == full_payload["balance_sheet"]
            assert rows[-1].payload["info"]["marketCap"] == 999_000_000
    finally:
        _cleanup(symbol)


# --- S-7(docs/daily_pipeline_throughput_plan_2026-09-04.md):決算通過をトリガーに
# した財務諸表の即時再取得 -----------------------------------------------------
#
# S-2は財務諸表を週次(月曜)化したが、実測(220件の変化中75件=34%が非月曜)で
# 「決算が週の半ばに出ると最大6日反映が遅れる」ことが分かった。`event_calendar`
# の次回決算日を過ぎたティッカーは、猶予日数(既定3日)以内なら週次日を待たず
# 財務諸表を再取得する(`_earnings_triggered_refetch`)。

_EARNINGS_WED = datetime.date(2099, 3, 4)  # 非月曜(水曜)の決算日


def _add_earnings_event(session, ticker_id: int, event_date: datetime.date) -> None:
    session.add(
        EventCalendar(
            ticker_id=ticker_id,
            event_type="earnings",
            event_date=event_date,
            is_estimated=True,
            source="yfinance",
            collected_on=event_date - datetime.timedelta(days=30),
        )
    )


def _seed_statement_snapshot(
    session, ticker_id: int, snapshot_date: datetime.date, statements_as_of: datetime.date | None, tag: str
) -> None:
    """財務諸表を含む直近raw_snapshotを1件仕込む(`_statements_as_of`つき)。"""
    payload = _financials_payload()
    if statements_as_of is not None:
        payload["_statements_as_of"] = statements_as_of.isoformat()
    session.add(
        RawSnapshot(
            ticker_id=ticker_id,
            snapshot_date=snapshot_date,
            source="yfinance",
            payload=payload,
            content_hash=f"s7-{tag}",
            last_seen_date=snapshot_date,
            available_from=snapshot_date,
        )
    )


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_earnings_date_in_window_triggers_statement_refetch_on_a_non_weekly_day(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """決算日(非月曜)が来て、直近の財務諸表取得日がそれより前なら、週次日
    でなくても財務諸表を再取得する。34%(75/220件)の非月曜変化が最大6日
    遅延していた問題そのものの再現。"""
    symbol = "ZZEARN1"
    _cleanup(symbol)
    try:
        assert _EARNINGS_WED.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            # 決算日より前(先週月曜)に取得済みの財務諸表
            _seed_statement_snapshot(
                session, ticker.id, _EARNINGS_WED - datetime.timedelta(days=7),
                statements_as_of=_EARNINGS_WED - datetime.timedelta(days=7), tag="earn1-prior",
            )
            _add_earnings_event(session, ticker.id, _EARNINGS_WED)
            config = _make_config(threshold=5)

            assert collect_one(session, uuid.uuid4(), symbol, config, _EARNINGS_WED) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_statement_refetch_repeats_within_grace_window_then_stops(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """2026-09-04監査で発見した欠陥の回帰テスト。

    以前の実装は`last_fetch < event_date`(決算日そのものと比較)で判定して
    いたため、決算日当日に1回fetchした瞬間`last_fetch == event_date`となり、
    `grace_days`の値に関係なく**猶予窓が実質1回で終わっていた**——
    「決算日当日だけ狙って外す」というS-7が防ぐはずだった失敗モードを、
    猶予窓を足したはずのコード自身が再現していた欠陥。

    正しい挙動は、決算日(E)から猶予窓の最終日(E+grace_days)までは
    yfinance側が決算をまだ反映していなくても**毎日再取得を試み続け**
    (反映ラグがいつ解消してもその日に捕まえる)、猶予窓を過ぎたら
    (E+grace_days+1)ぴたりと止まること。後半の「止まる」ところが、
    設計上の要点2(次の月曜まで毎日再取得し続けない)がまだ守られている
    ことの確認になっている。
    """
    symbol = "ZZEARN2"
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            _seed_statement_snapshot(
                session, ticker.id, _EARNINGS_WED - datetime.timedelta(days=7),
                statements_as_of=_EARNINGS_WED - datetime.timedelta(days=7), tag="earn2-prior",
            )
            _add_earnings_event(session, ticker.id, _EARNINGS_WED)
            config = _make_config(threshold=5)

        grace_days = config.statement_refresh_grace_days
        assert grace_days == 3  # 以降のオフセット計算はこの既定値を前提にする

        # E, E+1, E+2, E+3(猶予窓の最終日)まで:yfinance側がまだ決算を反映
        # していなくても(=毎回同じ内容を返しても)、毎日再取得を試みること。
        for offset in range(grace_days + 1):
            day = _EARNINGS_WED + datetime.timedelta(days=offset)
            assert day.weekday() != 0  # 月曜条件による別経路のTrueを混入させない
            with session_scope() as session:
                assert collect_one(session, uuid.uuid4(), symbol, config, day) == "success"
            _, kwargs = mock_fetch_financials.call_args
            assert kwargs.get("include_statements") is True, f"day E+{offset} should still refetch"

        # E+4(猶予窓を1日過ぎた日):ここでぴたりと止まる。
        past_window_day = _EARNINGS_WED + datetime.timedelta(days=grace_days + 1)
        assert past_window_day.weekday() != 0
        mock_fetch_financials.return_value = {"info": {"marketCap": 1_000_000_000}}

        with session_scope() as session:
            assert collect_one(session, uuid.uuid4(), symbol, config, past_window_day) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is False
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_ticker_without_event_calendar_row_keeps_the_weekly_baseline(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """`event_calendar`に行が無いティッカー(追跡対象外の大多数)は、S-7導入後も
    従来どおり週次日のみ財務諸表を取得する——カバレッジを広げないという
    タスク仕様どおりの挙動。"""
    symbol = "ZZEARN3"
    tuesday = datetime.date(2099, 3, 3)
    _cleanup(symbol)
    try:
        assert tuesday.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            _seed_statement_snapshot(
                session, ticker.id, tuesday - datetime.timedelta(days=7),
                statements_as_of=tuesday - datetime.timedelta(days=7), tag="earn3-prior",
            )
            # 意図的にEventCalendar行を追加しない
            config = _make_config(threshold=5)

            assert collect_one(session, uuid.uuid4(), symbol, config, tuesday) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is False
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_grace_window_boundary_still_triggers_on_the_last_day(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """猶予窓境界(設計上の要点3):既定`statement_refresh_grace_days=3`のとき、
    決算日+3日目(猶予窓の最終日)はまだ再取得の対象であること。"""
    symbol = "ZZEARN4"
    _cleanup(symbol)
    try:
        boundary_day = _EARNINGS_WED + datetime.timedelta(days=3)
        assert boundary_day.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            _seed_statement_snapshot(
                session, ticker.id, _EARNINGS_WED - datetime.timedelta(days=7),
                statements_as_of=_EARNINGS_WED - datetime.timedelta(days=7), tag="earn4-prior",
            )
            _add_earnings_event(session, ticker.id, _EARNINGS_WED)
            config = _make_config(threshold=5)

            assert collect_one(session, uuid.uuid4(), symbol, config, boundary_day) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_grace_window_boundary_does_not_trigger_the_day_after(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    """猶予窓境界の反対側:決算日+4日目(猶予窓を1日過ぎた日)は、まだ最新の
    財務諸表を取得していなくても再取得の対象にならないこと。ここで既に
    「取得済みだから止まる」自己収束(テスト2)とは別の経路——**そもそも
    猶予窓の外なので最初から対象外**——であることを、直近の財務諸表取得日を
    決算日より前のままにして確認する。"""
    symbol = "ZZEARN5"
    _cleanup(symbol)
    try:
        past_window_day = _EARNINGS_WED + datetime.timedelta(days=4)
        assert past_window_day.weekday() != 0
        mock_fetch_financials.return_value = _financials_payload()

        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            # 決算日より前に取得したまま(猶予窓の外なので、これがトリガーの
            # 対象になっていないことを純粋に検証できる)
            _seed_statement_snapshot(
                session, ticker.id, _EARNINGS_WED - datetime.timedelta(days=7),
                statements_as_of=_EARNINGS_WED - datetime.timedelta(days=7), tag="earn5-prior",
            )
            _add_earnings_event(session, ticker.id, _EARNINGS_WED)
            config = _make_config(threshold=5)

            assert collect_one(session, uuid.uuid4(), symbol, config, past_window_day) == "success"

        _, kwargs = mock_fetch_financials.call_args
        assert kwargs.get("include_statements") is False
    finally:
        _cleanup(symbol)


# --- Incremental shares-outstanding refresh ---------------------------------


def _seed_incremental_inputs(session, symbol: str, observed_at: datetime.date) -> Ticker:
    ticker = Ticker(symbol=symbol, market="US", cik="0000320193")
    session.add(ticker)
    session.flush()
    session.add(RawSnapshot(
        ticker_id=ticker.id,
        snapshot_date=observed_at,
        source="yfinance",
        payload=_financials_payload(),
        content_hash=f"shares-{symbol}",
        last_seen_date=observed_at,
        available_from=observed_at,
    ))
    session.add(PriceSnapshot(
        ticker_id=ticker.id,
        trade_date=observed_at,
        close=10.0,
        volume=1_000,
        shares_outstanding=1_000_000,
        shares_observed_at=observed_at,
        shares_coverage_status="collected_with_data",
    ))
    return ticker


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price")
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_recent_shares_are_carried_without_an_extra_shares_request(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    symbol = "ZZSHARES1"
    day = datetime.date(2099, 3, 4)  # Wednesday
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = {"info": {"marketCap": 1_000_000_000}}
        mock_fetch_price.return_value = {
            "trade_date": day,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 2_000,
            "shares_outstanding": None,
            "_shares_requested": False,
            "dividend": None,
            "recent_splits": [],
            "recent_closes": {},
        }
        with session_scope() as session:
            ticker = _seed_incremental_inputs(session, symbol, day - datetime.timedelta(days=1))
            config = _make_config(threshold=5)
            assert collect_one(
                session, uuid.uuid4(), symbol, config, day, market_session_date=day
            ) == "success"
            session.flush()
            current = session.query(PriceSnapshot).filter_by(
                ticker_id=ticker.id, trade_date=day
            ).one()
            assert current.shares_outstanding == 1_000_000
            assert current.shares_observed_at == day - datetime.timedelta(days=1)
            assert current.shares_coverage_status == "carried_forward"
        assert mock_fetch_price.call_args.kwargs["include_shares"] is False
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price")
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_new_sec_filing_triggers_immediate_shares_refresh(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    symbol = "ZZSHARES2"
    day = datetime.date(2099, 3, 4)
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = {"info": {"marketCap": 1_000_000_000}}
        mock_fetch_price.return_value = {
            "trade_date": day,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 2_000,
            "shares_outstanding": 2_000_000,
            "_shares_requested": True,
            "dividend": None,
            "recent_splits": [],
            "recent_closes": {},
        }
        with session_scope() as session:
            ticker = _seed_incremental_inputs(session, symbol, day - datetime.timedelta(days=1))
            session.add(Filing(
                ticker_id=ticker.id,
                cik=ticker.cik,
                accession_number="0000320193-99-000001",
                form="10-Q",
                filed_date=day,
            ))
            config = _make_config(threshold=5)
            assert collect_one(
                session, uuid.uuid4(), symbol, config, day, market_session_date=day
            ) == "success"
            session.flush()
            current = session.query(PriceSnapshot).filter_by(
                ticker_id=ticker.id, trade_date=day
            ).one()
            assert current.shares_outstanding == 2_000_000
            assert current.shares_observed_at == day
            assert current.shares_coverage_status == "collected_with_data"
        assert mock_fetch_price.call_args.kwargs["include_shares"] is True
    finally:
        _cleanup(symbol)


@patch("autoscreener.collectors.snapshot_collector.fetch_isin", return_value=None)
@patch("autoscreener.collectors.snapshot_collector.fetch_latest_price")
@patch("autoscreener.collectors.snapshot_collector.fetch_raw_financials")
def test_stale_provider_price_does_not_overwrite_historical_shares(
    mock_fetch_financials, mock_fetch_price, mock_fetch_isin
):
    symbol = "ZZSHARES3"
    day = datetime.date(2099, 3, 11)
    prior_day = day - datetime.timedelta(days=8)
    _cleanup(symbol)
    try:
        mock_fetch_financials.return_value = {"info": {"marketCap": 1_000_000_000}}
        mock_fetch_price.return_value = {
            "trade_date": prior_day,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 2_000,
            "shares_outstanding": 2_000_000,
            "_shares_requested": True,
            "dividend": None,
            "recent_splits": [],
            "recent_closes": {},
        }
        with session_scope() as session:
            ticker = _seed_incremental_inputs(session, symbol, prior_day)
            config = _make_config(threshold=5)
            assert collect_one(
                session, uuid.uuid4(), symbol, config, day, market_session_date=day
            ) == "success"
            session.flush()
            historical = session.query(PriceSnapshot).filter_by(
                ticker_id=ticker.id, trade_date=prior_day
            ).one()
            assert historical.shares_outstanding == 1_000_000
            assert historical.shares_observed_at == prior_day
        assert mock_fetch_price.call_args.kwargs["include_shares"] is True
    finally:
        _cleanup(symbol)
