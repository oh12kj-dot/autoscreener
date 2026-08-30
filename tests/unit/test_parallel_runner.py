"""並列収集ランナーのテスト(B-6、model_audit_v4_2026-08-26.md)。"""

import datetime
import uuid

from autoscreener.batch.parallel_runner import run_parallel
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
