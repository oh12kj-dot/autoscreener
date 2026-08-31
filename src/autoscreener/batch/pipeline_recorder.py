"""日次パイプラインの実行記録(14.15の運用監視。
docs/daily_job_status_screen_2026-08-30.md §4.1)。

`daily_pipeline.py` から工程単位で呼び出され、`pipeline_runs` /
`pipeline_stage_runs` に書き込む。**このモジュールは記録するだけで、
失敗を握り潰すか全体を止めるかの判断は一切持たない**——`stage()` は例外を
必ず再送出し、呼び出し側(`daily_pipeline.py`)の既存 try/except 構造を
そのまま尊重する(§9)。
"""

from __future__ import annotations

import datetime
import logging
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from autoscreener.db.models import PipelineRun, PipelineStageRun
from autoscreener.db.session import session_scope
from autoscreener.monitoring import CORE_STAGES, HealthFinding, determine_run_status

logger = logging.getLogger(__name__)

_ERROR_MESSAGE_MAX = 2000
_TRACEBACK_MAX = 8000

# §4.4:履歴を無限に伸ばさない。1日1実行×16行なので放置しても実害は小さいが、
# 上限を持たない表は作らない、という他テーブルの方針に揃える。
RETENTION_DAYS = 180


@dataclass
class StageHandle:
    """`PipelineRecorder.stage()` が yield するハンドル。

    呼び出し側は `handle.result = ...` に工程の戻り値(件数のdict)を入れる。
    設定しなければ `result=None` のまま `succeeded` として記録される。
    """

    result: dict | None = None


class PipelineRecorder:
    """パイプラインの工程を1つずつ記録する。

    記録は**工程ごとに即コミット**する。全部終わってからまとめて書くと、
    プロセスが途中で死んだ実行が丸ごと消え——それは2026-08-29型の障害で
    最も知りたい情報そのものである。`session_scope()` を工程ごとに開き、
    パイプライン本体の長大なトランザクションとは独立させる。
    """

    def __init__(self, run_date: datetime.date, is_weekly: bool, trigger: str = "scheduled") -> None:
        self.run_id = uuid.uuid4()
        self.run_date = run_date
        self.is_weekly = is_weekly
        self.trigger = trigger
        # run全体のstatus判定(§3.3)に使う。工程名→最終status。
        self._stage_statuses: dict[str, str] = {}

        with session_scope() as session:
            session.add(
                PipelineRun(
                    run_id=self.run_id,
                    run_date=run_date,
                    is_weekly=is_weekly,
                    trigger=trigger,
                    started_at=datetime.datetime.now(datetime.UTC),
                    status="running",
                )
            )
        logger.info("pipeline run started: run_id=%s run_date=%s", self.run_id, run_date)

    @contextmanager
    def stage(self, name: str, sequence: int) -> Iterator[StageHandle]:
        """開始時に `status="running"` で行を作り、終了時に成否を確定する(§4.1)。

        開始時点で行を作るのは、A節の「実行中(N/15工程完了)」やAPI応答が
        「今どの工程を実行中か」を見せられるようにするため——工程の所要時間が
        長い(収集・バックアップ等)場合、それが無いと実行中は該当工程の行が
        丸ごと存在せず「まだ始まっていない」のか「実行中」なのか区別できない。

        - 正常終了:`handle.result` を保存し `succeeded`
        - 例外送出:`failed` + 例外クラス名・メッセージ・tracebackを保存し、
          **例外は再送出する**(このヘルパは記録だけを担い、握り潰すか否かの
          判断は呼び出し側の既存 try/except に委ねる。§9)
        """
        started_at = datetime.datetime.now(datetime.UTC)
        self._stage_statuses[name] = "running"
        with session_scope() as session:
            session.add(
                PipelineStageRun(
                    run_id=self.run_id,
                    stage=name,
                    sequence=sequence,
                    status="running",
                    started_at=started_at,
                )
            )

        handle = StageHandle()
        try:
            yield handle
        except Exception as exc:
            self._finalize_stage(
                name,
                "failed",
                reason=type(exc).__name__,
                error_message=str(exc)[:_ERROR_MESSAGE_MAX],
                error_traceback=traceback.format_exc()[:_TRACEBACK_MAX],
            )
            raise
        else:
            self._finalize_stage(name, "succeeded", result=handle.result)

    def skip(self, name: str, sequence: int, reason: str) -> None:
        """週次工程が対象外の曜日であることを記録する(§4.1)。

        skipは一瞬で決まる(「開始したが未完了」の状態を経ない)ので、
        `stage()` と違って running を経由せず1回の書き込みで完結させる。
        """
        now = datetime.datetime.now(datetime.UTC)
        self._stage_statuses[name] = "skipped"
        with session_scope() as session:
            session.add(
                PipelineStageRun(
                    run_id=self.run_id,
                    stage=name,
                    sequence=sequence,
                    status="skipped",
                    started_at=now,
                    finished_at=now,
                    reason=reason,
                )
            )

    def _finalize_stage(
        self,
        name: str,
        status: str,
        *,
        result: dict | None = None,
        reason: str | None = None,
        error_message: str | None = None,
        error_traceback: str | None = None,
    ) -> None:
        """`stage()` が作った `running` 行を確定状態に更新する(§4.1)。"""
        self._stage_statuses[name] = status
        with session_scope() as session:
            row = session.query(PipelineStageRun).filter_by(run_id=self.run_id, stage=name).one()
            row.status = status
            row.finished_at = datetime.datetime.now(datetime.UTC)
            row.result = result
            row.reason = reason
            row.error_message = error_message
            row.error_traceback = error_traceback

    def non_core_failed_stages(self) -> list[str]:
        """中核工程(`monitoring.CORE_STAGES`)を除いた、failedで終わった工程名。

        §3.4 `stage_failed` 所見に使う。中核工程の失敗は run 全体を `failed`
        にする側(`determine_run_status`)で扱うため、ここでは含めない。
        """
        return [
            name
            for name, status in self._stage_statuses.items()
            if status == "failed" and name not in CORE_STAGES
        ]

    def previous_scored(self) -> int | None:
        """前回実行の scoring 工程の `scored` 件数(§3.4 `scoring_yield_dropped`)。

        今回より前の run_date のうち最新のものを見る。前回実行が無い、または
        前回の scoring が succeeded で終わっていない場合は None
        (「基準が無い」ことと「0件だった」ことを区別する。B-6と同じ判断)。
        """
        with session_scope() as session:
            row = (
                session.query(PipelineStageRun.result)
                .join(PipelineRun, PipelineRun.run_id == PipelineStageRun.run_id)
                .filter(
                    PipelineRun.run_date < self.run_date,
                    PipelineStageRun.stage == "scoring",
                    PipelineStageRun.status == "succeeded",
                )
                .order_by(PipelineRun.run_date.desc(), PipelineRun.started_at.desc())
                .first()
            )
        if row is None or row[0] is None:
            return None
        return row[0].get("scored")

    def finish(self, health: list[HealthFinding]) -> None:
        """全工程完了後に呼ぶ。run全体のstatusを確定し、health所見を保存する。"""
        status = determine_run_status(self._stage_statuses, health)
        with session_scope() as session:
            run = session.query(PipelineRun).filter_by(run_id=self.run_id).one()
            run.finished_at = datetime.datetime.now(datetime.UTC)
            run.status = status
            run.health = [
                {"code": f.code, "severity": f.severity, "message": f.message, "detail": f.detail} for f in health
            ]
        logger.info("pipeline run finished: run_id=%s status=%s findings=%d", self.run_id, status, len(health))

    def prune_old_runs(self) -> int:
        """§4.4:`run_date` が180日より前の `pipeline_runs` を削除する。

        `pipeline_stage_runs` はFKの `ondelete="CASCADE"` で一緒に消える。
        戻り値は削除した件数(呼び出し側でのログ用途)。
        """
        cutoff = self.run_date - datetime.timedelta(days=RETENTION_DAYS)
        with session_scope() as session:
            deleted = (
                session.query(PipelineRun)
                .filter(PipelineRun.run_date < cutoff)
                .delete(synchronize_session=False)
            )
        return deleted
