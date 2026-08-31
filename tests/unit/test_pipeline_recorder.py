"""tests/unit/test_pipeline_recorder.py

docs/daily_job_status_screen_2026-08-30.md §7。`docker compose up -d` で起動済みの
ローカル開発用Postgresに対して実行する(他の多くのバッチ系テストと同じ)。
実データと衝突しない未来日付(2098年以降)でテスト行を作り、終了時に削除する。
"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch.pipeline_recorder import PipelineRecorder
from autoscreener.db.models import PipelineRun, PipelineStageRun
from autoscreener.db.session import session_scope
from autoscreener.monitoring import HealthFinding

_TEST_RUN_DATE = datetime.date(2099, 1, 1)
_CLEANUP_FLOOR = datetime.date(2098, 1, 1)  # 前回実行系のテストが使う過去日も含めて掃除する


def _cleanup() -> None:
    with session_scope() as session:
        session.query(PipelineRun).filter(PipelineRun.run_date >= _CLEANUP_FLOOR).delete(synchronize_session=False)


@pytest.fixture(autouse=True)
def _around_each_test():
    _cleanup()
    yield
    _cleanup()


def test_stage_success_records_succeeded_with_result():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with recorder.stage("collection", 8) as st:
        st.result = {"success": 5}

    with session_scope() as session:
        row = session.query(PipelineStageRun).filter_by(run_id=recorder.run_id, stage="collection").one()
        assert row.status == "succeeded"
        assert row.result == {"success": 5}
        assert row.sequence == 8
        assert row.finished_at is not None


def test_stage_exception_records_failed_and_reraises():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)

    with pytest.raises(ValueError, match="boom"):
        with recorder.stage("gates", 9) as st:
            raise ValueError("boom")

    with session_scope() as session:
        row = session.query(PipelineStageRun).filter_by(run_id=recorder.run_id, stage="gates").one()
        assert row.status == "failed"
        assert row.result is None
        assert row.reason == "ValueError"
        assert row.error_message == "boom"
        assert "ValueError: boom" in row.error_traceback


def test_skip_records_skipped_with_reason():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    recorder.skip("macro", 3, "not_weekly")

    with session_scope() as session:
        row = session.query(PipelineStageRun).filter_by(run_id=recorder.run_id, stage="macro").one()
        assert row.status == "skipped"
        assert row.reason == "not_weekly"
        assert row.result is None


def test_stage_commits_immediately_visible_from_another_session():
    """全部終わってからまとめて書くと、プロセスが途中で死んだ実行が消える。
    それを防ぐのがこのテストの主旨(§4.1)——工程が完了する前でも、run行自体は
    別セッションから既に読める(コミット済み)ことを確認する。
    """
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)

    with session_scope() as other_session:
        run_row = other_session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert run_row.status == "running"
        assert run_row.finished_at is None

    with recorder.stage("collection", 8) as st:
        st.result = {"success": 1}
        with session_scope() as other_session:
            # withブロックの中(工程がまだ完了していない時点)でも、
            # runの行はコミット済みで読める。
            run_row = other_session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
            assert run_row.status == "running"

    with session_scope() as session:
        stage_row = session.query(PipelineStageRun).filter_by(run_id=recorder.run_id, stage="collection").one()
        assert stage_row.status == "succeeded"


def test_finish_sets_run_status_and_health():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with recorder.stage("collection", 8) as st:
        st.result = {"success": 1}

    finding = HealthFinding(code="stage_failed", severity="warning", message="msg", detail={"stage": "filings"})
    recorder.finish([finding])

    with session_scope() as session:
        run_row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert run_row.status == "degraded"
        assert run_row.finished_at is not None
        assert run_row.health == [
            {"code": "stage_failed", "severity": "warning", "message": "msg", "detail": {"stage": "filings"}}
        ]


def test_finish_with_no_findings_and_no_failures_is_succeeded():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with recorder.stage("collection", 8) as st:
        st.result = {"success": 1}
    recorder.finish([])

    with session_scope() as session:
        run_row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert run_row.status == "succeeded"
        assert run_row.health == []


def test_finish_with_core_stage_failure_is_failed():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with pytest.raises(RuntimeError):
        with recorder.stage("scoring", 11) as st:
            raise RuntimeError("boom")
    recorder.finish([])

    with session_scope() as session:
        run_row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert run_row.status == "failed"


def test_previous_scored_uses_most_recent_prior_run():
    earlier_date = _TEST_RUN_DATE - datetime.timedelta(days=1)
    earlier = PipelineRecorder(earlier_date, is_weekly=False)
    with earlier.stage("scoring", 11) as st:
        st.result = {"scored": 1204}
    earlier.finish([])

    later = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    assert later.previous_scored() == 1204


def test_previous_scored_none_when_no_prior_run():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    assert recorder.previous_scored() is None


def test_previous_scored_ignores_failed_scoring_stage():
    earlier_date = _TEST_RUN_DATE - datetime.timedelta(days=1)
    earlier = PipelineRecorder(earlier_date, is_weekly=False)
    with pytest.raises(RuntimeError):
        with earlier.stage("scoring", 11) as st:
            raise RuntimeError("boom")

    later = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    assert later.previous_scored() is None


def test_non_core_failed_stages_excludes_core_stages():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with pytest.raises(ValueError):
        with recorder.stage("collection", 8) as st:
            raise ValueError("core failure")
    with pytest.raises(RuntimeError):
        with recorder.stage("filings", 13) as st:
            raise RuntimeError("non-core failure")

    assert recorder.non_core_failed_stages() == ["filings"]


def test_prune_old_runs_deletes_beyond_retention_but_keeps_recent():
    old_date = _TEST_RUN_DATE - datetime.timedelta(days=200)
    old_recorder = PipelineRecorder(old_date, is_weekly=False)
    old_recorder.finish([])

    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    recorder.prune_old_runs()

    with session_scope() as session:
        assert session.query(PipelineRun).filter_by(run_id=old_recorder.run_id).one_or_none() is None
        # pipeline_stage_runs はCASCADEで一緒に消えているはず(§3.2)。
        assert session.query(PipelineRun).filter_by(run_id=recorder.run_id).one_or_none() is not None
