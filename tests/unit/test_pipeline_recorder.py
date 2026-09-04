"""tests/unit/test_pipeline_recorder.py

docs/daily_job_status_screen_2026-08-30.md §7。`docker compose up -d` で起動済みの
ローカル開発用Postgresに対して実行する(他の多くのバッチ系テストと同じ)。
実データより前の隔離日付(1900年)でテスト行を作り、終了時にその年だけ削除する。
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from autoscreener.batch.pipeline_recorder import PipelineRecorder, sweep_orphan_runs
from autoscreener.db.models import PipelineRun, PipelineStageRun
from autoscreener.db.session import session_scope
from autoscreener.monitoring import HealthFinding

_TEST_RUN_DATE = datetime.date(1900, 1, 2)
_CLEANUP_START = datetime.date(1900, 1, 1)  # 前回実行系テストの1日前を含む
_CLEANUP_END = datetime.date(1900, 12, 31)


def _cleanup() -> None:
    with session_scope() as session:
        session.query(PipelineRun).filter(
            PipelineRun.run_date >= _CLEANUP_START,
            PipelineRun.run_date <= _CLEANUP_END,
        ).delete(synchronize_session=False)


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


@patch("autoscreener.batch.pipeline_recorder.session_scope")
def test_prune_old_runs_uses_retention_cutoff_without_touching_shared_history(mock_scope):
    """未来日テストから共有DBの実運用履歴を削除しない。"""
    session = mock_scope.return_value.__enter__.return_value
    delete = session.query.return_value.filter.return_value.delete
    delete.return_value = 3
    recorder = object.__new__(PipelineRecorder)
    recorder.run_date = _TEST_RUN_DATE

    assert recorder.prune_old_runs() == 3
    delete.assert_called_once_with(synchronize_session=False)


def test_constructor_seeds_last_heartbeat_at_from_started_at():
    """A-2:起動直後をheartbeat初回とみなす(工程が始まる前にプロセスが
    死んだ場合でも、sweeperが `started_at` 基準と同じ扱いで拾えるように
    するため)。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert row.last_heartbeat_at is not None
        assert row.last_heartbeat_at == row.started_at


def test_heartbeat_advances_last_heartbeat_at():
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with session_scope() as session:
        before = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one().last_heartbeat_at

    recorder.heartbeat()

    with session_scope() as session:
        after = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one().last_heartbeat_at
    assert after > before


def test_stage_boundary_advances_heartbeat():
    """A-2:`stage()` の開始・終了ごとにheartbeatが進むこと。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with session_scope() as session:
        initial = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one().last_heartbeat_at

    with recorder.stage("collection", 8) as st:
        st.result = {"success": 1}

    with session_scope() as session:
        after_stage = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one().last_heartbeat_at
    assert after_stage >= initial


def test_finish_with_exception_marks_run_failed_and_records_health():
    """A-2の中核(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.3):
    core stageの例外が `run_daily_pipeline()` 自身から抜けた場合でも、
    `pipeline_runs` を `running` のまま残さない。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with pytest.raises(RuntimeError):
        with recorder.stage("gates", 10) as st:
            raise RuntimeError("FK violation (simulated)")

    recorder.finish_with_exception(RuntimeError("FK violation (simulated)"))

    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert row.status == "failed"
        assert row.status != "running"
        assert row.finished_at is not None
        codes = [h["code"] for h in (row.health or [])]
        assert "run_unhandled_exception" in codes


def test_sweep_orphan_runs_aborts_stale_running_run():
    """A-2の受け入れ条件:heartbeatが閾値以上進んでいない `running` runは
    `aborted` に落ちる。2026-09-03の停止runが手作業のUPDATEなしに次回実行で
    回収されることの直接検証。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    stale_heartbeat = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=200)
    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        row.last_heartbeat_at = stale_heartbeat
        row.started_at = stale_heartbeat

    swept = sweep_orphan_runs(threshold=datetime.timedelta(minutes=90))
    assert recorder.run_id in swept

    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert row.status == "aborted"
        assert row.finished_at is not None
        codes = [h["code"] for h in (row.health or [])]
        assert "run_orphaned_swept" in codes


def test_sweep_orphan_runs_leaves_fresh_running_run_alone():
    """heartbeatが最近進んでいる `running` runは、単に長く動いているだけの
    正常な実行かもしれないので回収しない。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    recorder.heartbeat()  # 直近のheartbeat

    swept = sweep_orphan_runs(threshold=datetime.timedelta(minutes=90))
    assert recorder.run_id not in swept

    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert row.status == "running"
        assert row.finished_at is None


def test_resume_reuses_run_id_and_preserves_succeeded_stage():
    """A-6の核心(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.3
    「2時間超のcollection後にgateで落ちても、checkpoint/resumeが無い」):
    前回succeededした工程を、再開後にもう一度実行しなくて済むこと。"""
    original = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with original.stage("collection", 8) as st:
        st.result = {"success": 100}
    with pytest.raises(RuntimeError):
        with original.stage("gates", 10) as st:
            raise RuntimeError("FK violation (simulated)")
    original.finish_with_exception(RuntimeError("FK violation (simulated)"))

    resumed = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False, resume_run_id=original.run_id)
    assert resumed.run_id == original.run_id
    previous_results = resumed.resumed_stage_results()
    assert previous_results == {"collection": {"success": 100}}
    assert "gates" not in previous_results  # failedした工程は再利用対象に含まない

    with session_scope() as session:
        run_row = session.query(PipelineRun).filter_by(run_id=original.run_id).one()
        assert run_row.status == "running"  # 再開でrunningへ戻る
        assert run_row.finished_at is None


def test_resume_retries_a_previously_failed_stage_without_unique_violation():
    """A-6:`(run_id, stage)` のUNIQUE制約があっても、再開後に前回failedした
    工程を同じrun_idの下で再試行できること(重複insertエラーにならない)。"""
    original = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    with pytest.raises(RuntimeError):
        with original.stage("gates", 10) as st:
            raise RuntimeError("boom")
    original.finish_with_exception(RuntimeError("boom"))

    resumed = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False, resume_run_id=original.run_id)
    with resumed.stage("gates", 10) as st:  # 同じ(run_id, stage)を再試行
        st.result = {"included": 5}

    with session_scope() as session:
        row = session.query(PipelineStageRun).filter_by(run_id=original.run_id, stage="gates").one()
        assert row.status == "succeeded"
        assert row.result == {"included": 5}
        assert row.error_message is None  # 前回の失敗情報が残っていない


def test_resume_skip_does_not_duplicate_a_previously_skipped_stage():
    """A-6:再開時に同じ週次skip判定を再度記録しても重複insertエラーに
    ならないこと。"""
    original = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    original.skip("macro", 3, "not_weekly")
    with pytest.raises(RuntimeError):
        with original.stage("gates", 10) as st:
            raise RuntimeError("boom")
    original.finish_with_exception(RuntimeError("boom"))

    resumed = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False, resume_run_id=original.run_id)
    resumed.skip("macro", 3, "not_weekly")  # 再度呼んでも例外にならない

    with session_scope() as session:
        row = session.query(PipelineStageRun).filter_by(run_id=original.run_id, stage="macro").one()
        assert row.status == "skipped"


def test_sweep_orphan_runs_ignores_already_terminal_runs():
    """既に `succeeded`/`failed` で確定したrunは、heartbeatが古くても対象外
    (`status == "running"` フィルタで除外される)。"""
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    recorder.finish([])  # succeeded で確定
    stale_heartbeat = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=200)
    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        row.last_heartbeat_at = stale_heartbeat

    swept = sweep_orphan_runs(threshold=datetime.timedelta(minutes=90))
    assert recorder.run_id not in swept

    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        assert row.status == "succeeded"
