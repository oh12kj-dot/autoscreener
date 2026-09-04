"""A-1(docs/defect_and_edge_audit_2026-08-28.md D-12)のテスト:

  1. `business_days_between` の境界
  2. `run_scoring` のデータ鮮度ガード(`_check_price_freshness`)
  3. サーキットブレーカー作動時の consecutive_failures ロールバック

`docker compose up -d` のローカル開発用Postgresに対して実行する
(test_apply_gates_point_in_time.py と同じ方針)。ZZ*** シンボルを使い後片付けする。
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from autoscreener.config import (
    CircuitBreakerConfig,
    CollectionConfig,
    FreshnessConfig,
    QuarantineConfig,
    RetryConfig,
    load_scoring_config,
)
from autoscreener.dates import business_days_between
from autoscreener.db.models import CollectionLog, PriceSnapshot, Ticker, UniverseSnapshot
from autoscreener.db.session import session_scope
from autoscreener.scoring.engine import _check_price_freshness


# ---------------------------------------------------------------------------
# 1. business_days_between
# ---------------------------------------------------------------------------
def test_business_days_between_same_day_is_zero():
    d = datetime.date(2026, 8, 26)
    assert business_days_between(d, d) == 0


def test_business_days_between_skips_weekend():
    # 金 2026-08-28 -> 月 2026-08-31 は営業日1つ(土日を数えない)
    assert business_days_between(datetime.date(2026, 8, 28), datetime.date(2026, 8, 31)) == 1


def test_business_days_between_counts_consecutive_weekdays():
    # 月->金 は4営業日
    assert business_days_between(datetime.date(2026, 8, 24), datetime.date(2026, 8, 28)) == 4


def test_business_days_between_is_signed():
    assert business_days_between(datetime.date(2026, 8, 28), datetime.date(2026, 8, 24)) == -4


# ---------------------------------------------------------------------------
# 2. run_scoring のデータ鮮度ガード
# ---------------------------------------------------------------------------
_SYMBOL_A = "ZZFRESH1"
_SYMBOL_B = "ZZFRESH2"

# 2026-08-30:カバレッジ判定の2テストは、以前は `score_date` に本物の
# `PriceSnapshot` の最新日(`_latest_price_date`)をそのまま使っていた。
# `_check_price_freshness` の `included_ids` / `max_price_date` はどちらも
# **symbolで絞り込まない全体集計**なので、本物の日付を使うと本番の
# `universe_snapshots`(実データでは同日に1000件超)がそのままカバレッジの
# 分母に混ざり、テストが作った1〜2銘柄では計算結果を制御できない
# (実際に2026-08-30、開発DBの実データにより
# `insufficient_price_coverage (1.8% of 1261 ...)` でカバレッジ「合格」を
# 検証するテストが落ちた)。
#
# 本物の価格データは未来日を持たない(取引日は当日までしか存在しない)ので、
# 未来日を使えばテスト専用の行だけがそこに存在することを保証できる——
# このファイル末尾のサーキットブレーカーのテストが `2099-01-01` を使うのと
# 同じ発想(衝突を避けるため別の日にしてある)。
_FRESH_SCORE_DATE = datetime.date(2099, 4, 1)


def _cleanup_freshness() -> None:
    with session_scope() as session:
        for ticker in session.query(Ticker).filter(Ticker.symbol.in_([_SYMBOL_A, _SYMBOL_B])).all():
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(UniverseSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


# WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):以前は「DBに
# 既存の価格行がある」前提で `func.max(PriceSnapshot.trade_date)` を読んでいた
# が、隔離済みテストDB(`autoscreener_test`)は空で始まるため `None` を返し
# `None + timedelta(...)` で `TypeError` になっていた。自前で1件だけ
# PriceSnapshot を作り、その日付を基準にする——`_check_price_freshness` が
# symbolで絞り込まない全体最大値を見る点は `_FRESH_SCORE_DATE` のコメントと
# 同じなので、他テストの日付(2099-01-01 / 2099-04-01)より後の日付を使い
# 「自分の行が確実に全体最大になる」ことを保証する。
_STALE_SYMBOL = "ZZFRESHSTALE"
_STALE_PRICE_DATE = datetime.date(2100, 1, 1)


def test_freshness_guard_flags_stale_price_data():
    """`score_date` が最新価格から `max_price_staleness_days` を超えて離れていれば中止する。"""
    scoring_config = load_scoring_config()
    with session_scope() as session:
        ticker = Ticker(symbol=_STALE_SYMBOL, market="US")
        session.add(ticker)
        session.flush()
        session.add(PriceSnapshot(ticker_id=ticker.id, trade_date=_STALE_PRICE_DATE, close=10.0))
    try:
        far_future = _STALE_PRICE_DATE + datetime.timedelta(days=60)
        with session_scope() as session:
            reason = _check_price_freshness(session, far_future, scoring_config)
        assert reason is not None
        assert reason.startswith("stale_price_data")
    finally:
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=_STALE_SYMBOL).one_or_none()
            if ticker is not None:
                session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
                session.delete(ticker)


def test_freshness_guard_flags_low_same_day_coverage():
    """当日ゲート通過銘柄の一部しか最新取引日の価格行を持たないなら中止する。

    `score_date` は本物データと衝突しない未来日(`_FRESH_SCORE_DATE`)を使う
    ——理由は`_FRESH_SCORE_DATE` のコメントを参照。
    """
    _cleanup_freshness()
    score_date = _FRESH_SCORE_DATE
    scoring_config = load_scoring_config().model_copy(
        update={"freshness": FreshnessConfig(max_price_staleness_days=2, min_same_day_price_coverage=0.9)}
    )
    try:
        with session_scope() as session:
            a = Ticker(symbol=_SYMBOL_A, market="US")
            b = Ticker(symbol=_SYMBOL_B, market="US")
            session.add_all([a, b])
            session.flush()
            # 両方ゲート通過扱い。A だけが最新取引日の価格行を持つ(coverage 50%)。
            session.add_all(
                [
                    UniverseSnapshot(snapshot_date=score_date, ticker_id=a.id, included=True),
                    UniverseSnapshot(snapshot_date=score_date, ticker_id=b.id, included=True),
                    PriceSnapshot(ticker_id=a.id, trade_date=score_date, close=10.0),
                ]
            )

        with session_scope() as session:
            reason = _check_price_freshness(session, score_date, scoring_config)
        assert reason is not None
        assert reason.startswith("insufficient_price_coverage")
    finally:
        _cleanup_freshness()


def test_freshness_guard_passes_when_fresh_and_covered():
    """`score_date` は本物データと衝突しない未来日(`_FRESH_SCORE_DATE`)を使う
    ——理由は`_FRESH_SCORE_DATE` のコメントを参照。"""
    _cleanup_freshness()
    score_date = _FRESH_SCORE_DATE
    scoring_config = load_scoring_config().model_copy(
        update={"freshness": FreshnessConfig(max_price_staleness_days=2, min_same_day_price_coverage=0.9)}
    )
    try:
        with session_scope() as session:
            a = Ticker(symbol=_SYMBOL_A, market="US")
            session.add(a)
            session.flush()
            session.add_all(
                [
                    UniverseSnapshot(snapshot_date=score_date, ticker_id=a.id, included=True),
                    PriceSnapshot(ticker_id=a.id, trade_date=score_date, close=10.0),
                ]
            )
        with session_scope() as session:
            reason = _check_price_freshness(session, score_date, scoring_config)
        assert reason is None
    finally:
        _cleanup_freshness()


# ---------------------------------------------------------------------------
# 3. サーキットブレーカー作動時の consecutive_failures ロールバック
# ---------------------------------------------------------------------------
def _rollback_config() -> CollectionConfig:
    return CollectionConfig(
        max_workers=4,
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=20, failure_rate_threshold=0.5),
        quarantine=QuarantineConfig(consecutive_failure_threshold=5, retry_interval_days=7),
    )


def test_circuit_breaker_rolls_back_consecutive_failures():
    """広域障害でブレーカーが作動したら、その実行で積んだ failure 増分を巻き戻し、
    その実行のせいで隔離された銘柄を解放する(A-1 / D-12)。"""
    from autoscreener.batch.parallel_runner import run_parallel

    symbols = [f"ZZCB{i}" for i in range(60)]
    threshold = _rollback_config().quarantine.consecutive_failure_threshold
    _cleanup_cb(symbols)
    try:
        with session_scope() as session:
            # 隔離閾値の1つ手前から始める。この実行が +1 して閾値に達し隔離する。
            tickers = [
                Ticker(symbol=s, market="US", consecutive_failures=threshold - 1, is_quarantined=False)
                for s in symbols
            ]
            session.add_all(tickers)

        def failing_worker(symbol: str, run_id: uuid.UUID) -> str:
            with session_scope() as session:
                ticker = session.query(Ticker).filter_by(symbol=symbol).one()
                ticker.consecutive_failures += 1
                if ticker.consecutive_failures >= threshold:
                    ticker.is_quarantined = True
                session.add(
                    CollectionLog(
                        run_id=run_id,
                        ticker_id=ticker.id,
                        snapshot_date=datetime.date(2099, 1, 1),
                        status="transient_failure",
                        detail=None,
                    )
                )
            return "transient_failure"

        counts = run_parallel(
            symbols,
            failing_worker,
            _rollback_config(),
            datetime.date(2099, 1, 1),
            rollback_consecutive_failures_on_trip=True,
        )
        assert counts.get("failures_rolled_back", 0) > 0

        with session_scope() as session:
            failed_ids = {
                row[0]
                for row in session.query(CollectionLog.ticker_id)
                .join(Ticker, Ticker.id == CollectionLog.ticker_id)
                .filter(Ticker.symbol.in_(symbols), CollectionLog.status == "transient_failure")
                .all()
            }
            assert failed_ids, "expected some failures before the breaker tripped"
            rows = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
            for t in rows:
                if t.id in failed_ids:
                    # この実行での +1 が巻き戻り、この実行のせいの隔離も解除される。
                    assert t.consecutive_failures == threshold - 1
                    assert t.is_quarantined is False
    finally:
        _cleanup_cb(symbols)


def _cleanup_cb(symbols: list[str]) -> None:
    with session_scope() as session:
        for ticker in session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all():
            session.query(CollectionLog).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)
        session.query(CollectionLog).filter(
            CollectionLog.status.in_(["run_started", "run_finished", "circuit_breaker_tripped", "failures_rolled_back"]),
            CollectionLog.ticker_id.is_(None),
            CollectionLog.snapshot_date == datetime.date(2099, 1, 1),
        ).delete(synchronize_session=False)
