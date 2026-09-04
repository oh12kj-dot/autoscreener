"""tests/unit/test_collect_consensus.py(S-3・S-4、
docs/daily_pipeline_throughput_plan_2026-09-04.md)。

S-3:`YfinanceConsensusProvider.fetch`は以前、`targetMeanPrice`1項目のためだけ
に`obj.info`へアクセスしHTTPを1本(quoteSummary)投げていた。同じ`info`は
collection工程が数時間前に取得して`raw_snapshots.payload["info"]`へ保存済み
なので、そこから読んで渡す経路に変えた。

S-4:`collect_consensus`は以前`for ticker in query.all():`の完全な逐次ループ
で、`run_parallel`も共有リミッターも通っていなかった。S-1(リミッターの
HTTP単位化)とS-3の後であれば、共有`yfinance`リミッター配下で並列化しても
上限を超えない構造になっている。

DBに触れる(ローカル開発用Postgres、他のunitテストと同じ方針)。専用シンボル
(ZZ***)を使い、終了時に削除する。
"""

from __future__ import annotations

import datetime
import time

import pytest

from autoscreener.batch.collect_consensus import collect_consensus
from autoscreener.collectors.consensus import YfinanceConsensusProvider
from autoscreener.config import CircuitBreakerConfig, CollectionConfig, QuarantineConfig, RetryConfig
from autoscreener.db.models import AnalystConsensusSnapshot, RawSnapshot, Ticker
from autoscreener.db.session import session_scope

_SYMBOLS = ["ZZCONS1", "ZZCONS2", "ZZCONS3"]


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(_SYMBOLS)).all()
        for ticker in tickers:
            session.query(AnalystConsensusSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


def _config(max_workers: int = 4) -> CollectionConfig:
    return CollectionConfig(
        max_workers=max_workers,
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        yfinance_requests_per_second=1000.0,  # テストでは天井そのものは対象外
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=1, failure_rate_threshold=0.99),
        quarantine=QuarantineConfig(consecutive_failure_threshold=5, retry_interval_days=7),
    )


# S-4監査(2026-09-04)で見つかった「スロットルのインストールがimport順序の
# 偶然に依存していた」欠陥(このモジュールも実例の1つだった)の回帰テストは、
# 同じ欠陥クラスの他の実例(`collectors/calendar_source.py`等)とまとめて
# `tests/unit/test_yfinance_throttle_import_isolation.py`に集約してある。


# ---------------------------------------------------------------------------
# S-3:`.info`のHTTP取得を廃止し、DBの直近payloadから読む
# ---------------------------------------------------------------------------


def test_yfinance_provider_never_touches_ticker_info():
    """`.info`にアクセスしたら例外を出すダミーを使い、S-3後は`.info`へ
    アクセスしなくなった(=HTTPが1本減った)ことを確認する。"""

    class EstimateTable:
        empty = False

        def iterrows(self):
            return iter((("0y", {"avg": 10, "low": 9, "high": 11, "numberOfAnalysts": 3}),))

    class _NoInfoTicker:
        revenue_estimate = EstimateTable()
        earnings_estimate = None

        @property
        def info(self):  # pragma: no cover - 呼ばれたら即失敗させるためのガード
            raise AssertionError(".info にアクセスした(S-3で廃止したはずのHTTP呼び出し)")

    as_of = datetime.datetime(2026, 8, 31, tzinfo=datetime.timezone.utc)
    rows = YfinanceConsensusProvider(lambda _: _NoInfoTicker()).fetch("TEST", as_of, target_mean_price=42.0)
    assert rows[0].target_price_mean == pytest.approx(42.0)


def test_collect_consensus_reads_target_mean_price_from_latest_raw_snapshot():
    """collectionが数時間前に保存した`raw_snapshots.payload["info"]`から
    `targetMeanPrice`を読み、providerへ渡すこと(2本目のHTTPを廃止した代替経路)。"""
    symbol = "ZZCONS1"
    _cleanup()
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            session.add(
                RawSnapshot(
                    ticker_id=ticker.id,
                    snapshot_date=datetime.date(2026, 8, 30),
                    source="yfinance",
                    payload={"info": {"targetMeanPrice": 123.45}},
                    content_hash="zzcons1-h1",
                    last_seen_date=datetime.date(2026, 8, 30),
                    available_from=datetime.date(2026, 8, 30),
                )
            )

        seen_prices: list[float | None] = []

        class _RecordingProvider:
            name = "recording"

            def fetch(self, ticker, as_of, target_mean_price=None):
                seen_prices.append(target_mean_price)
                return []

        stats = collect_consensus(_RecordingProvider(), symbols=[symbol], collection_config=_config())
        assert stats["processed"] == 1
        assert seen_prices == [123.45]
    finally:
        _cleanup()


def test_collect_consensus_passes_none_when_no_raw_snapshot_exists():
    """まだ収集されていない(raw_snapshotが無い)銘柄はNoneを渡す
    (`targetMeanPrice`不明として扱う。既存のNone欠損ポリシーに委ねる)。"""
    symbol = "ZZCONS2"
    _cleanup()
    try:
        with session_scope() as session:
            session.add(Ticker(symbol=symbol, market="US"))

        seen_prices: list[float | None] = ["sentinel"]

        class _RecordingProvider:
            name = "recording"

            def fetch(self, ticker, as_of, target_mean_price=None):
                seen_prices[0] = target_mean_price
                return []

        collect_consensus(_RecordingProvider(), symbols=[symbol], collection_config=_config())
        assert seen_prices == [None]
    finally:
        _cleanup()


# ---------------------------------------------------------------------------
# S-4:並列化
# ---------------------------------------------------------------------------


def test_collect_consensus_runs_tickers_concurrently():
    """逐次ループ(旧実装)ならN銘柄×sleep秒かかる。並列化されていれば
    それより大幅に速く終わる。"""
    _cleanup()
    try:
        with session_scope() as session:
            for symbol in _SYMBOLS:
                session.add(Ticker(symbol=symbol, market="US"))

        sleep_seconds = 0.3

        class _SlowProvider:
            name = "slow"

            def fetch(self, ticker, as_of, target_mean_price=None):
                time.sleep(sleep_seconds)
                return []

        start = time.monotonic()
        stats = collect_consensus(
            _SlowProvider(), symbols=_SYMBOLS, collection_config=_config(max_workers=len(_SYMBOLS))
        )
        elapsed = time.monotonic() - start

        assert stats["processed"] == len(_SYMBOLS)
        # 逐次なら sleep_seconds * len(_SYMBOLS) = 0.9秒はかかる。並列なら
        # 1回分強で済むはず。
        assert elapsed < sleep_seconds * len(_SYMBOLS) * 0.7, (
            f"{elapsed:.2f}s かかった(逐次ループに戻っていないか確認すること)"
        )
    finally:
        _cleanup()


def test_collect_consensus_isolates_one_tickers_failure_from_others():
    """1銘柄が失敗しても他の銘柄の処理・集計は正常に進むこと(並列化後も
    銘柄ごとに独立したセッション/トランザクションであることの確認)。"""
    _cleanup()
    try:
        with session_scope() as session:
            for symbol in _SYMBOLS:
                session.add(Ticker(symbol=symbol, market="US"))

        class _FlakyProvider:
            name = "flaky"

            def fetch(self, ticker, as_of, target_mean_price=None):
                if ticker == _SYMBOLS[0]:
                    raise ValueError("boom")
                return []

        stats = collect_consensus(_FlakyProvider(), symbols=_SYMBOLS, collection_config=_config())
        assert stats["processed"] == len(_SYMBOLS)
        assert stats["failed"] == 1
        assert stats["no_finding"] == len(_SYMBOLS) - 1
    finally:
        _cleanup()
