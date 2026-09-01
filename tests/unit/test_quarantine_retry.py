"""隔離リストからの復帰経路(18.1)。

18.1は隔離を「日次の通常対象から除外。週次で再挑戦し、復旧すれば自動復帰
(無限リトライ・**無限スキップ**の両方を回避)」と定めている。呼び出し元が
`is_quarantined == False` で無条件に絞っていたため、後半——再挑戦——が
一度も起きず、隔離された銘柄が永久に収集対象外になっていた。
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from autoscreener.batch.run_daily_collection import (
    collection_population_counts,
    select_collectable_symbols,
)
from autoscreener.config import (
    CircuitBreakerConfig,
    CollectionConfig,
    QuarantineConfig,
    RetryConfig,
)
from autoscreener.db.models import Base, Ticker

RETRY_INTERVAL_DAYS = 7


def _config() -> CollectionConfig:
    return CollectionConfig(
        max_workers=1,
        request_jitter_min_seconds=0,
        request_jitter_max_seconds=0,
        retry=RetryConfig(max_attempts=1, backoff_base_seconds=0.01, backoff_max_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(min_sample_size=50, failure_rate_threshold=0.3),
        quarantine=QuarantineConfig(
            consecutive_failure_threshold=5, retry_interval_days=RETRY_INTERVAL_DAYS
        ),
    )


@pytest.fixture
def session():
    # JSONB を使うテーブルは作らない(SQLiteに存在しない型のため)。
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Ticker.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as s:
        yield s


def _ticker(symbol: str, *, quarantined: bool, days_since_attempt: float | None) -> Ticker:
    last_attempted_at = (
        None
        if days_since_attempt is None
        else datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_since_attempt)
    )
    return Ticker(
        symbol=symbol,
        market="US",
        consecutive_failures=5 if quarantined else 0,
        is_quarantined=quarantined,
        last_attempted_at=last_attempted_at,
    )


def test_healthy_tickers_are_always_collected(session):
    session.add(_ticker("OK", quarantined=False, days_since_attempt=0.1))
    session.flush()

    assert select_collectable_symbols(session, _config()) == ["OK"]


def test_quarantined_ticker_is_retried_once_the_interval_has_elapsed(session):
    session.add(_ticker("BACK", quarantined=True, days_since_attempt=RETRY_INTERVAL_DAYS + 1))
    session.flush()

    # ここが空になるのが以前のバグ:隔離解除は収集の成功時にしか起きないため、
    # 対象から外し続けると復帰する手段が存在しない。
    assert select_collectable_symbols(session, _config()) == ["BACK"]


def test_quarantined_ticker_is_skipped_inside_the_retry_interval(session):
    session.add(_ticker("WAIT", quarantined=True, days_since_attempt=RETRY_INTERVAL_DAYS - 2))
    session.flush()

    assert select_collectable_symbols(session, _config()) == []


def test_quarantined_ticker_without_an_attempt_timestamp_is_retried(session):
    session.add(_ticker("NEVER", quarantined=True, days_since_attempt=None))
    session.flush()

    assert select_collectable_symbols(session, _config()) == ["NEVER"]


def test_delisted_ticker_is_not_retried_or_counted_as_live_quarantine(session):
    live = _ticker("LIVE", quarantined=False, days_since_attempt=0.1)
    delisted = _ticker(
        "DEAD", quarantined=True, days_since_attempt=RETRY_INTERVAL_DAYS + 1
    )
    delisted.delisted_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
    session.add_all([live, delisted])
    session.flush()

    assert select_collectable_symbols(session, _config()) == ["LIVE"]
    assert collection_population_counts(session) == (0, 1)
