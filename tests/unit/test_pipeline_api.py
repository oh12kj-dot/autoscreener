"""tests/unit/test_pipeline_api.py

`GET /api/v1/pipeline/runs` / `GET /api/v1/pipeline/runs/{run_id}` のテスト
(14.15、docs/daily_job_status_screen_2026-08-30.md §5・§7)。

`docker compose up -d` で起動済みのローカル開発用Postgresに対して実行する。
実データと衝突しない未来日付でテスト行を作り、終了時に削除する。
"""

from __future__ import annotations

import datetime
import uuid

from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.api.routes import get_pipeline_run
from autoscreener.batch.pipeline_recorder import PipelineRecorder
from autoscreener.db.models import PipelineRun
from autoscreener.db.session import get_session_factory, session_scope

client = TestClient(app)

_TEST_RUN_DATE = datetime.date(2097, 6, 1)


def _cleanup(run_id: uuid.UUID) -> None:
    with session_scope() as session:
        session.query(PipelineRun).filter_by(run_id=run_id).delete(synchronize_session=False)


def test_latest_with_zero_records_returns_empty_not_404():
    """§5.2:初回導入直後、記録がまだ1件も無い状態は404ではなく空を返す。

    共有の開発用DBには他のテストが作った行が既にありうるため、**コミットしない
    削除**で「記録ゼロ件」を実DBを壊さずに再現する(テスト後は必ずrollback)。
    書き込みロール(`db.session`)のセッションを直接使う——APIの読み取り専用
    ロールにはDELETE権限が無いため。
    """
    session = get_session_factory()()
    try:
        session.query(PipelineRun).delete(synchronize_session=False)
        result = get_pipeline_run("latest", session)
        assert result.run is None
        assert result.stages == []
    finally:
        session.rollback()
        session.close()


def test_list_and_detail_reflect_a_seeded_run():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    try:
        with recorder.stage("collection", 8) as st:
            st.result = {"success": 5, "sanitized": 1, "quarantined": 2, "universe_size": 10}
        with recorder.stage("gates", 9) as st:
            st.result = {"included": 4, "excluded": 2}
        with recorder.stage("scoring", 11) as st:
            st.result = {"scored": 4, "unmeasurable": 0}
        recorder.skip("macro", 3, "not_weekly")
        recorder.finish([])

        run_id = str(recorder.run_id)

        list_body = client.get("/api/v1/pipeline/runs?limit=50").json()
        assert "history_starts_at" in list_body
        entry = next(r for r in list_body["runs"] if r["run_id"] == run_id)
        assert entry["status"] == "succeeded"
        assert entry["headline"]["collected"] == 6  # success(5) + sanitized(1)
        assert entry["headline"]["gated_in"] == 4
        assert entry["headline"]["scored"] == 4
        assert entry["headline"]["quarantined"] == 2
        assert entry["headline"]["universe_size"] == 10
        assert entry["stage_summary"]["succeeded"] == 3
        assert entry["stage_summary"]["skipped"] == 1
        assert entry["expected_stage_count"] > sum(entry["stage_summary"].values())

        detail_body = client.get(f"/api/v1/pipeline/runs/{run_id}").json()
        assert detail_body["run"]["run_id"] == run_id
        stages = {s["stage"]: s for s in detail_body["stages"]}
        assert stages["collection"]["status"] == "succeeded"
        assert stages["collection"]["result"]["success"] == 5
        assert stages["macro"]["status"] == "skipped"
        assert stages["macro"]["reason"] == "not_weekly"

        latest_body = client.get("/api/v1/pipeline/runs/latest").json()
        # 他のテストが後から同日/より新しい実行を作っている可能性があるため、
        # 「200で何かしらの実行が返る」ことだけを確認する(この実行そのものが
        # 最新である保証は無い)。
        assert latest_body["run"] is not None
    finally:
        _cleanup(recorder.run_id)


def test_failed_stage_exposes_error_detail_via_api():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    try:
        try:
            with recorder.stage("filings", 13) as st:
                raise ValueError("EDGAR_USER_AGENT is not set")
        except ValueError:
            pass
        recorder.finish([])

        body = client.get(f"/api/v1/pipeline/runs/{recorder.run_id}").json()
        filings = next(s for s in body["stages"] if s["stage"] == "filings")
        assert filings["status"] == "failed"
        assert filings["reason"] == "ValueError"
        assert filings["error_message"] == "EDGAR_USER_AGENT is not set"
        assert "ValueError" in filings["error_traceback"]
    finally:
        _cleanup(recorder.run_id)


def test_unknown_run_id_returns_404():
    response = client.get(f"/api/v1/pipeline/runs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_malformed_run_id_returns_422():
    response = client.get("/api/v1/pipeline/runs/not-a-uuid")
    assert response.status_code == 422


def test_orphaned_run_is_reported_as_failed_with_finding():
    """§4.3:`finished_at` がNULLのまま6時間超過した実行は、DBを書き換えずAPI
    応答でのみ `failed` + `run_orphaned` として返す。"""
    run_id = uuid.uuid4()
    started_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=7)
    with session_scope() as session:
        session.add(
            PipelineRun(
                run_id=run_id,
                run_date=_TEST_RUN_DATE,
                is_weekly=False,
                trigger="scheduled",
                started_at=started_at,
                finished_at=None,
                status="running",
                health=None,
            )
        )
    try:
        response = client.get(f"/api/v1/pipeline/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["run"]["status"] == "failed"
        assert body["run"]["finished_at"] is None
        codes = {f["code"] for f in body["run"]["health"]}
        assert "run_orphaned" in codes
    finally:
        _cleanup(run_id)


def test_recent_running_run_is_not_treated_as_orphaned():
    run_id = uuid.uuid4()
    started_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    with session_scope() as session:
        session.add(
            PipelineRun(
                run_id=run_id,
                run_date=_TEST_RUN_DATE,
                is_weekly=False,
                trigger="scheduled",
                started_at=started_at,
                finished_at=None,
                status="running",
                health=None,
            )
        )
    try:
        body = client.get(f"/api/v1/pipeline/runs/{run_id}").json()
        assert body["run"]["status"] == "running"
        assert body["run"]["health"] == []
    finally:
        _cleanup(run_id)
