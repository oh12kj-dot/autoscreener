"""A-5のテスト(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.3/10.4)。

`docker compose up -d` で起動済みのローカル開発用Postgres(専用テストDB)に
対して実行する。`build_operational_readiness(session)` を**直接**呼ぶ
(`TestClient` 経由のHTTPリクエストにしない)——理由は
`test_pipeline_api.py` の `test_latest_with_zero_records_returns_empty_not_404`
と同じで、HTTPリクエストはFastAPIの依存性注入で**別のDBセッション**を
作るため、このテストが行う「未コミットの削除で0件状態を再現する」トリックが
効かない(別セッションからは見えない)。同一セッションへ直接渡せる関数に
分離してあるのはこのため(`operational_readiness.py` のdocstring参照)。

すべて未コミットの変更として行い、最後に必ず `rollback()` する——共有の
テストDBへ他のテストが作った行を壊さない。
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from autoscreener.api.operational_readiness import build_operational_readiness
from autoscreener.db.models import ModelRun, PipelineRun, RawSnapshot, Score, Ticker, UniverseSnapshot
from autoscreener.db.session import get_session_factory

_TODAY = datetime.date(2098, 1, 15)


@pytest.fixture
def write_session():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_no_data_anywhere_is_degraded_with_specific_reasons(write_session):
    """記録が1件も無い状態(新規導入直後)は `degraded` になり、理由が
    黙って空になったりしない(監査§10.4修正案7)。"""
    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)
    session.query(UniverseSnapshot).delete(synchronize_session=False)
    session.query(Score).delete(synchronize_session=False)
    session.query(RawSnapshot).delete(synchronize_session=False)
    session.query(ModelRun).delete(synchronize_session=False)

    result = build_operational_readiness(session, today=_TODAY)

    assert result["status"] == "degraded"
    assert "no_pipeline_run_recorded" in result["reasons"]
    assert "universe_snapshots_never_populated" in result["reasons"]
    assert "scores_never_populated" in result["reasons"]
    assert "raw_snapshots_never_populated" in result["reasons"]
    assert "model_scores_never_populated" in result["reasons"]
    assert result["pipeline"]["has_run"] is False
    assert all(item["days_stale"] is None for item in result["dataset_freshness"])


def test_healthy_recent_run_and_fresh_data_is_ready(write_session):
    """A-5受け入れ条件の対照実験:直近succeededのrun + 当日データがあれば
    `ready` になる(=過剰検知しないことの確認)。"""
    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)
    session.query(UniverseSnapshot).delete(synchronize_session=False)
    session.query(Score).delete(synchronize_session=False)
    session.query(RawSnapshot).delete(synchronize_session=False)
    session.query(ModelRun).delete(synchronize_session=False)

    ticker = Ticker(symbol="ZZOPREADY", market="US", sector="Technology")
    session.add(ticker)
    session.flush()

    now = datetime.datetime.now(datetime.UTC)
    session.add(
        PipelineRun(
            run_id=uuid.uuid4(),
            run_date=_TODAY,
            is_weekly=False,
            trigger="scheduled",
            started_at=now - datetime.timedelta(minutes=30),
            finished_at=now - datetime.timedelta(minutes=5),
            last_heartbeat_at=now - datetime.timedelta(minutes=5),
            status="succeeded",
            health=[],
        )
    )
    session.add(UniverseSnapshot(snapshot_date=_TODAY, ticker_id=ticker.id, included=True, exclusion_reason=None))
    session.add(
        Score(ticker_id=ticker.id, score_date=_TODAY, scoring_version="v4-test", config_hash="deadbeef")
    )
    session.add(
        RawSnapshot(
            ticker_id=ticker.id,
            snapshot_date=_TODAY,
            source="test",
            payload={},
            content_hash="raw-hash",
            last_seen_date=_TODAY,
            available_from=_TODAY,
            is_valid=True,
        )
    )
    session.add(
        ModelRun(
            id=uuid.uuid4(),
            model_version="v5",
            config_hash="deadbeef",
            as_of=_TODAY,
            mode="shadow",
            status="succeeded",
            population_count=1,
            started_at=now - datetime.timedelta(minutes=20),
            finished_at=now - datetime.timedelta(minutes=10),
        )
    )
    session.flush()

    result = build_operational_readiness(session, today=_TODAY)

    assert result["status"] == "ready", result["reasons"]
    assert result["reasons"] == []
    assert result["pipeline"]["status"] == "succeeded"
    for item in result["dataset_freshness"]:
        assert item["days_stale"] == 0


def test_stuck_running_run_is_degraded(write_session):
    """A-2との連携:heartbeatが90分以上更新されない `running` runは
    `/operational-readiness` からも degraded として見える
    (sweeperがまだ回収していない間の窓を埋める)。"""
    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)

    now = datetime.datetime.now(datetime.UTC)
    stuck_run_id = uuid.uuid4()
    session.add(
        PipelineRun(
            run_id=stuck_run_id,
            run_date=_TODAY,
            is_weekly=False,
            trigger="scheduled",
            started_at=now - datetime.timedelta(hours=3),
            finished_at=None,
            last_heartbeat_at=now - datetime.timedelta(hours=2),
            status="running",
            health=[],
        )
    )
    session.flush()

    result = build_operational_readiness(session, today=_TODAY)

    assert result["status"] == "degraded"
    assert "latest_run_stuck_running" in result["reasons"]
    assert result["pipeline"]["run_id"] == str(stuck_run_id)


def test_recently_started_running_run_is_not_flagged_stuck(write_session):
    """heartbeatが最近進んでいる `running` runは、単に動いているだけなので
    degraded理由に挙げない(過剰検知の防止)。"""
    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)

    now = datetime.datetime.now(datetime.UTC)
    session.add(
        PipelineRun(
            run_id=uuid.uuid4(),
            run_date=_TODAY,
            is_weekly=False,
            trigger="scheduled",
            started_at=now - datetime.timedelta(minutes=5),
            finished_at=None,
            last_heartbeat_at=now - datetime.timedelta(seconds=30),
            status="running",
            health=[],
        )
    )
    session.flush()

    result = build_operational_readiness(session, today=_TODAY)

    assert "latest_run_stuck_running" not in result["reasons"]
    assert result["pipeline"]["status"] == "running"


def test_failed_run_is_degraded(write_session):
    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)

    now = datetime.datetime.now(datetime.UTC)
    session.add(
        PipelineRun(
            run_id=uuid.uuid4(),
            run_date=_TODAY,
            is_weekly=False,
            trigger="scheduled",
            started_at=now - datetime.timedelta(hours=1),
            finished_at=now - datetime.timedelta(minutes=50),
            last_heartbeat_at=now - datetime.timedelta(minutes=50),
            status="failed",
            health=[],
        )
    )
    session.flush()

    result = build_operational_readiness(session, today=_TODAY)

    assert result["status"] == "degraded"
    assert "latest_run_failed" in result["reasons"]


def test_ready_endpoint_contract_is_unchanged():
    """A-5の受け入れ条件:`/ready` は従来通り200であること(意味を分ける)。

    HTTP経由で確認する(こちらは実際のcontractの検証なので、意図的に
    `TestClient` を使う)。
    """
    from fastapi.testclient import TestClient

    from autoscreener.api.main import app

    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_operational_readiness_endpoint_returns_200_even_when_degraded(write_session):
    """`/operational-readiness` は degraded でもHTTPとしては200を返す
    (可用性プローブではなく状態レポートであるため、main.py のdocstring参照)。
    """
    from fastapi.testclient import TestClient

    from autoscreener.api.main import app

    session = write_session
    session.query(PipelineRun).delete(synchronize_session=False)
    session.flush()
    # このテストはHTTP経由(別セッション)で叩くため、上の未コミット削除は
    # 直接は効かない——ここでは「degraded であっても500にならない」という
    # HTTP契約だけを確認する(実際のdegraded判定ロジックは他のテストが
    # 同一セッション経由で検証済み)。
    client = TestClient(app)
    response = client.get("/operational-readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ready", "degraded")
    assert "reasons" in body
    assert "pipeline" in body
    assert "dataset_freshness" in body
    assert "alembic" in body
