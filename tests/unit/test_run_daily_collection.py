from unittest.mock import MagicMock, patch

from autoscreener.batch.run_daily_collection import run_daily_collection
from autoscreener.config import (
    CircuitBreakerConfig,
    CollectionConfig,
    QuarantineConfig,
    RetryConfig,
)


def _make_config(min_sample: int, threshold: float) -> CollectionConfig:
    return CollectionConfig(
        max_workers=1,  # 単一ワーカーにして完了順を決定的にする(テストの再現性のため)
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=min_sample, failure_rate_threshold=threshold),
        quarantine=QuarantineConfig(consecutive_failure_threshold=5, retry_interval_days=7),
    )


def _fake_collect_one(session, run_id, symbol, collection_config, snapshot_date):
    return "transient_failure" if symbol.startswith("FAIL") else "success"


@patch("autoscreener.batch.parallel_runner.session_scope")
@patch("autoscreener.batch.run_daily_collection.session_scope")
@patch("autoscreener.batch.run_daily_collection.collect_one", side_effect=_fake_collect_one)
def test_circuit_breaker_trips_and_cancels_remaining_work(mock_collect_one, mock_worker_session, mock_breaker_session):
    mock_worker_session.return_value.__enter__.return_value = MagicMock()
    mock_breaker_session.return_value.__enter__.return_value = MagicMock()

    # 3 success + 2 fail = 5件処理時点で失敗率40% (>=30%の閾値) となり中断されるはず
    symbols = ["OK1", "OK2", "OK3", "FAIL1", "FAIL2", "OK4", "OK5", "OK6"]
    result = run_daily_collection(symbols, collection_config=_make_config(min_sample=5, threshold=0.3))

    total_processed = sum(result.values())
    assert total_processed < len(symbols)
    assert result.get("transient_failure") == 2

    added_objects = [call.args[0] for call in mock_breaker_session.return_value.__enter__.return_value.add.call_args_list]
    tripped_logs = [obj for obj in added_objects if getattr(obj, "status", None) == "circuit_breaker_tripped"]
    assert len(tripped_logs) == 1


@patch("autoscreener.batch.parallel_runner.session_scope")
@patch("autoscreener.batch.run_daily_collection.session_scope")
@patch("autoscreener.batch.run_daily_collection.collect_one", side_effect=_fake_collect_one)
def test_circuit_breaker_does_not_trip_below_threshold(mock_collect_one, mock_worker_session, mock_breaker_session):
    mock_worker_session.return_value.__enter__.return_value = MagicMock()
    mock_breaker_session.return_value.__enter__.return_value = MagicMock()

    # 1 fail out of 10 = 10% < 30%の閾値なので中断されないはず
    symbols = ["FAIL1"] + [f"OK{i}" for i in range(9)]
    result = run_daily_collection(symbols, collection_config=_make_config(min_sample=5, threshold=0.3))

    assert sum(result.values()) == len(symbols)
    added_objects = [call.args[0] for call in mock_breaker_session.return_value.__enter__.return_value.add.call_args_list]
    assert not any(getattr(obj, "status", None) == "circuit_breaker_tripped" for obj in added_objects)
