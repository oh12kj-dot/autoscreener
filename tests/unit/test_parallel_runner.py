"""tests/unit/test_parallel_runner.py(B-6、docs/model_audit_v4_2026-08-26.md;
S-5、docs/daily_pipeline_throughput_plan_2026-09-04.md)。

前半は元からある `run_parallel`(yfinance収集向け、ジッタ・サーキット
ブレーカー・`CollectionLog`記録を持つ)のテスト。

後半(S-5)は `run_parallel_tickers`——litigation/filing_sections/dilution/
customer_concentrationの4工程が共有する、より軽量な並列実行ヘルパー
(銘柄ごとの逐次ループをやめ、共有`sec`リミッター配下で並列化する)の
テスト。その共通機構そのものを検証することで、4工程それぞれで同じ性質を
毎回書き直して確認する必要が無いようにする。

DBに触れる(`session_scope()`を経由するだけで、`run_parallel_tickers`側は
テーブルへの読み書きはしない)。
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid

import pytest

from autoscreener.batch.parallel_runner import run_parallel, run_parallel_tickers
from autoscreener.config import CircuitBreakerConfig, CollectionConfig, QuarantineConfig, RetryConfig
from autoscreener.db.models import CollectionLog
from autoscreener.db.session import session_scope


def _config() -> CollectionConfig:
    return CollectionConfig(
        max_workers=2,
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=100, failure_rate_threshold=0.9),
        quarantine=QuarantineConfig(consecutive_failure_threshold=5, retry_interval_days=7),
    )


def test_run_parallel_emits_start_and_finish_markers_with_target_count():
    """B-6: `/universe/status` が「実行中」と「完了」を区別できるよう、
    バッチ開始時に対象件数を、終了時に完了マーカーをログへ残す。"""
    symbols = ["ZZP1", "ZZP2", "ZZP3"]

    captured_run_id: uuid.UUID | None = None

    def worker(symbol: str, run_id: uuid.UUID) -> str:
        nonlocal captured_run_id
        captured_run_id = run_id
        return "success"

    run_parallel(symbols, worker, _config(), datetime.date.today())

    assert captured_run_id is not None
    with session_scope() as session:
        logs = session.query(CollectionLog).filter_by(run_id=captured_run_id).all()
        statuses = {log.status: log for log in logs}
        assert "run_started" in statuses
        assert statuses["run_started"].detail == {"target_count": 3}
        assert "run_finished" in statuses
        assert statuses["run_finished"].detail["processed"] == 3
        assert statuses["run_finished"].detail["circuit_breaker_tripped"] is False
        session.query(CollectionLog).filter_by(run_id=captured_run_id).delete()


def test_unhandled_error_counts_toward_circuit_breaker():
    """E-3: worker_fnが分類外の例外を投げ続けた場合、`unhandled_error` が失敗として
    数えられ、サーキットブレーカーが作動して未着手分がキャンセルされること。"""
    symbols = [f"ZZE{i}" for i in range(200)]

    captured_run_id: uuid.UUID | None = None

    def always_raises(symbol: str, run_id: uuid.UUID) -> str:
        nonlocal captured_run_id
        captured_run_id = run_id
        raise RuntimeError("boom")

    status_counts = run_parallel(symbols, always_raises, _config(), datetime.date.today())

    assert status_counts.get("unhandled_error", 0) > 0
    assert captured_run_id is not None
    with session_scope() as session:
        logs = session.query(CollectionLog).filter_by(run_id=captured_run_id).all()
        statuses = {log.status: log for log in logs}
        assert "circuit_breaker_tripped" in statuses
        assert statuses["run_finished"].detail["circuit_breaker_tripped"] is True
        # 200件すべては処理されず、途中で打ち切られていること。
        assert statuses["run_finished"].detail["processed"] < len(symbols)
        session.query(CollectionLog).filter_by(run_id=captured_run_id).delete()


# ---------------------------------------------------------------------------
# S-5: run_parallel_tickers(EDGAR系工程向けの軽量並列実行ヘルパー)
# ---------------------------------------------------------------------------


def test_workers_run_concurrently_not_sequentially():
    """逐次ループ(旧実装)ならN件×sleep秒かかる。並列化されていればそれより
    大幅に速く終わる——litigation実測(299銘柄で実質0.26 req/秒=設定上限の
    5%)の原因が「レート制限ではなく並列度ゼロだったこと」の再現。"""
    sleep_seconds = 0.3
    ticker_ids = [1, 2, 3, 4]

    def worker(session, ticker_id: int) -> dict[str, int]:
        time.sleep(sleep_seconds)
        return {"tickers": 1}

    start = time.monotonic()
    totals = run_parallel_tickers(ticker_ids, worker, max_workers=len(ticker_ids))
    elapsed = time.monotonic() - start

    assert totals == {"tickers": len(ticker_ids)}
    assert elapsed < sleep_seconds * len(ticker_ids) * 0.7, (
        f"{elapsed:.2f}s かかった(逐次ループに戻っていないか確認すること)"
    )


def test_each_worker_gets_its_own_session():
    """SQLAlchemyのSessionはスレッドセーフではないため、銘柄ごとに専用の
    `session_scope()`が渡ること(全ワーカーが同一セッションを共有していないこと)。

    `id()`だけを集めて比較すると、セッションがすぐ解放され別オブジェクトに
    同じアドレスが再利用されて偽陽性になりうる(CPythonの仕様)ため、
    オブジェクト自体をリストで保持してGCされないようにする。
    """
    seen_sessions: list[object] = []
    lock = threading.Lock()

    def worker(session, ticker_id: int) -> dict[str, int]:
        with lock:
            seen_sessions.append(session)  # 参照を保持し続けてGC・id再利用を防ぐ
        return {"tickers": 1}

    run_parallel_tickers([1, 2, 3], worker, max_workers=3)
    assert len({id(s) for s in seen_sessions}) == 3


def test_results_are_summed_across_tickers_by_key():
    def worker(session, ticker_id: int) -> dict[str, int]:
        return {"tickers": 1, "new_events": ticker_id, "existing": 0}

    totals = run_parallel_tickers([1, 2, 3], worker, max_workers=3)
    assert totals == {"tickers": 3, "new_events": 6, "existing": 0}


def test_empty_ticker_list_returns_empty_totals():
    def worker(session, ticker_id: int) -> dict[str, int]:  # pragma: no cover - 呼ばれないはず
        raise AssertionError("空リストなのにworkerが呼ばれた")

    assert run_parallel_tickers([], worker, max_workers=5) == {}


def test_unexpected_worker_exception_propagates():
    """`worker_fn`内で本当に想定外の例外(プログラムのバグ)が起きたら、
    黙って握りつぶさずここで再送出すること。4つのEDGARバッチはいずれも
    `worker_fn`自身の中で`CollectionError`/`Exception`を個別に握って
    `failures`カウントへ変換しているので、ここまで漏れてくるのは
    本当にバグのときだけ、というのが前提。"""

    def worker(session, ticker_id: int) -> dict[str, int]:
        if ticker_id == 2:
            raise RuntimeError("boom")
        return {"tickers": 1}

    with pytest.raises(RuntimeError, match="boom"):
        run_parallel_tickers([1, 2, 3], worker, max_workers=3)
