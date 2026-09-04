"""A-6のCLIテスト(docs/racr_wp_a_operational_safety_2026-09-04.md)。

`run-daily-pipeline --resume` と `sweep-orphan-runs` を追加した
(監査§10.3「長時間collectionの後にgateで落ちても、checkpoint/resumeが無い」・
§10.4修正案5)。`--resume` 自体の実質的な効果(前回succeededした工程を
再実行しない)は `tests/unit/test_daily_pipeline.py` が
`run_daily_pipeline(resume=True)` を直接呼んで検証済みなので、ここでは
CLI層(typerの引数配線)だけを見る。
"""

from __future__ import annotations

import datetime

from typer.testing import CliRunner
from unittest.mock import patch

from autoscreener.batch.pipeline_recorder import PipelineRecorder
from autoscreener.cli import app
from autoscreener.db.models import PipelineRun
from autoscreener.db.session import session_scope

runner = CliRunner()

_TEST_RUN_DATE = datetime.date(2098, 3, 1)


def _cleanup() -> None:
    with session_scope() as session:
        session.query(PipelineRun).filter_by(run_date=_TEST_RUN_DATE).delete(synchronize_session=False)


def test_run_daily_pipeline_resume_flag_is_passed_through():
    """`--resume` を付けたら `run_daily_pipeline(resume=True)` が呼ばれ、
    付けなければ `resume=False`(既定)で呼ばれること。"""
    with patch("autoscreener.cli.run_daily_pipeline", return_value={}) as mock_run:
        result = runner.invoke(app, ["run-daily-pipeline", "--resume"])
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once_with(resume=True)

    with patch("autoscreener.cli.run_daily_pipeline", return_value={}) as mock_run:
        result = runner.invoke(app, ["run-daily-pipeline"])
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once_with(resume=False)


def test_sweep_orphan_runs_cmd_aborts_a_stale_running_run():
    """`sweep-orphan-runs` CLIが実際に孤児runを `aborted` へ落とすこと。"""
    _cleanup()
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=200)
    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        row.last_heartbeat_at = stale
        row.started_at = stale

    try:
        result = runner.invoke(app, ["sweep-orphan-runs"])
        assert result.exit_code == 0, result.output
        assert str(recorder.run_id) in result.output

        with session_scope() as session:
            row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
            assert row.status == "aborted"
    finally:
        _cleanup()


def test_sweep_orphan_runs_cmd_reports_none_when_nothing_stuck():
    _cleanup()
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    recorder.finish([])  # succeeded、対象外

    try:
        result = runner.invoke(app, ["sweep-orphan-runs"])
        assert result.exit_code == 0, result.output
        assert "ありませんでした" in result.output
    finally:
        _cleanup()


def test_sweep_orphan_runs_cmd_accepts_custom_threshold_minutes():
    """`--threshold-minutes` で閾値を上書きできること。"""
    _cleanup()
    recorder = PipelineRecorder(_TEST_RUN_DATE, is_weekly=False)
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    with session_scope() as session:
        row = session.query(PipelineRun).filter_by(run_id=recorder.run_id).one()
        row.last_heartbeat_at = stale
        row.started_at = stale

    try:
        # 既定(90分)では対象外だが、5分にすれば対象になる。
        result_default = runner.invoke(app, ["sweep-orphan-runs"])
        assert result_default.exit_code == 0
        assert "ありませんでした" in result_default.output

        result_custom = runner.invoke(app, ["sweep-orphan-runs", "--threshold-minutes", "5"])
        assert result_custom.exit_code == 0, result_custom.output
        assert str(recorder.run_id) in result_custom.output
    finally:
        _cleanup()
