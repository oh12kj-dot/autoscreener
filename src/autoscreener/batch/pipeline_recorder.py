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

import sqlalchemy as sa

from autoscreener.db.models import PipelineRun, PipelineStageRun
from autoscreener.db.session import session_scope
from autoscreener.monitoring import CORE_STAGES, HealthFinding, determine_run_status

logger = logging.getLogger(__name__)

_ERROR_MESSAGE_MAX = 2000
_TRACEBACK_MAX = 8000

# §4.4:履歴を無限に伸ばさない。1日1実行×16行なので放置しても実害は小さいが、
# 上限を持たない表は作らない、という他テーブルの方針に揃える。
RETENTION_DAYS = 180

# A-2(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.3
# 「2時間超のcollection後にgateで落ちても、checkpoint/resumeが無い」・
# §10.4の修正案4):heartbeatがこの時間以上更新されない `running` runは、
# プロセスが死んだとみなして `aborted` へ落とす。25工程を逐次実行する現行の
# 日次パイプラインは、収集工程だけで数十分〜1時間規模になりうる
# (`config/collection.yaml` のワーカー数・レート上限次第)ため、
# 短すぎる閾値は「動いているだけの実行」を誤って回収してしまう。
# API層の孤児判定(§4.3)が採用している6時間よりは短く設定する——
# あちらは「表示上、確実に死んでいるとみなせる」閾値であるのに対し、
# こちらは「次のheartbeatが来るまでの正常な間隔」を基準にするため、
# 工程1つぶんの所要時間に数倍の余裕を持たせた90分で十分小さい。
DEFAULT_ORPHAN_SWEEP_THRESHOLD = datetime.timedelta(minutes=90)


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

    def __init__(
        self,
        run_date: datetime.date,
        is_weekly: bool,
        trigger: str = "scheduled",
        *,
        resume_run_id: uuid.UUID | None = None,
    ) -> None:
        """`resume_run_id` を渡すと、新しい行を作らず既存のrunを再開する
        (A-6、docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.3
        「2時間超のcollection後にgateで落ちても、checkpoint/resumeが無い」)。

        再開時は既存の `pipeline_stage_runs` から工程ごとの最終statusを
        読み込み、`self._stage_statuses` を初期化する——`finish()` の
        run全体status判定・`non_core_failed_stages()` は、今回のプロセスで
        一度も触っていない(=前回succeededのまま再実行しなかった)工程の
        statusも合わせて評価する必要があるため。
        """
        self.run_date = run_date
        self.is_weekly = is_weekly
        self.trigger = trigger
        # run全体のstatus判定(§3.3)に使う。工程名→最終status。
        self._stage_statuses: dict[str, str] = {}

        if resume_run_id is not None:
            self.run_id = resume_run_id
            with session_scope() as session:
                existing_stages = session.query(PipelineStageRun).filter_by(run_id=self.run_id).all()
                self._stage_statuses = {s.stage: s.status for s in existing_stages}
                run = session.query(PipelineRun).filter_by(run_id=self.run_id).one()
                # 前回 failed/aborted で終わった行を、再試行中であることが
                # わかるように running へ戻す。`finished_at` も再確定まで
                # クリアする(中途半端な「終わっているのに動いている」表示を
                # 避ける)。
                run.status = "running"
                run.finished_at = None
            self.heartbeat()
            logger.info(
                "pipeline run resumed: run_id=%s run_date=%s already_recorded_stages=%s",
                self.run_id,
                run_date,
                sorted(self._stage_statuses),
            )
            return

        self.run_id = uuid.uuid4()
        started_at = datetime.datetime.now(datetime.UTC)
        with session_scope() as session:
            session.add(
                PipelineRun(
                    run_id=self.run_id,
                    run_date=run_date,
                    is_weekly=is_weekly,
                    trigger=trigger,
                    started_at=started_at,
                    # A-2:起動直後をheartbeat初回とみなす。工程が始まる前に
                    # プロセスが死んだ場合でも(collection開始前など)、
                    # sweeperが `started_at` 基準と同じ扱いで拾えるようにする。
                    last_heartbeat_at=started_at,
                    status="running",
                )
            )
        logger.info("pipeline run started: run_id=%s run_date=%s", self.run_id, run_date)

    def resumed_stage_results(self) -> dict[str, dict]:
        """既に `succeeded` した工程の `result` を返す(A-6)。

        呼び出し側(`daily_pipeline.py`)は、ここに含まれる工程名について
        実際の処理を再実行せず、この結果をそのまま使う——2時間かかる
        collectionを、その後段のgateが落ちただけで毎回捨てないための
        本体。`resume_run_id` を渡していないPipelineRecorder(=新規run)では
        常に空dictを返す(前回の実行という概念が無いため)。
        """
        with session_scope() as session:
            rows = (
                session.query(PipelineStageRun)
                .filter_by(run_id=self.run_id, status="succeeded")
                .all()
            )
        return {row.stage: (row.result or {}) for row in rows}

    def heartbeat(self) -> None:
        """このrunがまだ生きていることを刻む(A-2)。

        `stage()`/`skip()` の境界ごとに呼ぶ。`sweep_orphan_runs()` は
        この時刻が `DEFAULT_ORPHAN_SWEEP_THRESHOLD` 以上進んでいない
        `running` runを、プロセスが死んだものとして `aborted` に落とす。
        """
        with session_scope() as session:
            session.query(PipelineRun).filter_by(run_id=self.run_id).update(
                {"last_heartbeat_at": datetime.datetime.now(datetime.UTC)}
            )

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
            # A-6:再開(resume_run_id)で同じrun_idの下、前回failed/running
            # (孤児)だった工程を再試行するとき、`(run_id, stage)` の
            # UNIQUE制約により新規insertはできない——既存行を運用中の状態へ
            # 戻す(上書きする)。通常(新規run)は該当行が無いのでinsertに
            # なり、挙動は従来と同じ。
            existing = session.query(PipelineStageRun).filter_by(run_id=self.run_id, stage=name).one_or_none()
            if existing is None:
                session.add(
                    PipelineStageRun(
                        run_id=self.run_id,
                        stage=name,
                        sequence=sequence,
                        status="running",
                        started_at=started_at,
                    )
                )
            else:
                existing.status = "running"
                existing.started_at = started_at
                existing.finished_at = None
                existing.result = None
                existing.reason = None
                existing.error_message = None
                existing.error_traceback = None
        # A-2:工程開始のたびにheartbeatを刻む。収集のように1工程が長時間
        # かかる場合でも、少なくとも「その工程に入った」時刻までは
        # sweeperが生存とみなす。
        self.heartbeat()

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
            # A-6:再開時、同じ日のうちに再度 `skip()` が呼ばれても
            # (`(run_id, stage)` UNIQUE制約があるため)重複insertにしない。
            existing = session.query(PipelineStageRun).filter_by(run_id=self.run_id, stage=name).one_or_none()
            if existing is None:
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
            else:
                existing.status = "skipped"
                existing.finished_at = now
                existing.reason = reason
        self.heartbeat()  # A-2:週次スキップの連続でも生存を刻んでおく。

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
        self.heartbeat()  # A-2:工程完了時にも刻む(次工程開始までの空白を縮める)。

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

    def finish_with_exception(self, exc: BaseException) -> None:
        """A-2(監査§10.3「core stageの例外が `run_daily_pipeline()` 外へ出ると
        `recorder.finish()` に到達せず、runが永久にrunning」への対応)。

        `collection`/`gates`/`scoring`/`forward_validation` は意図的に個別の
        try/exceptで囲んでいない(停止則。モジュールdocstring§9参照)ため、
        その例外は `run_daily_pipeline()` 自身から抜けうる——このメソッドは
        `daily_pipeline.py` 側の outer try/except から呼ばれ、通常の
        `finish()` に届く前にプロセスが停止する場合でも `pipeline_runs` を
        `running` のまま残さない。呼び出し側はこの後で例外を再送出し、
        CLI/スケジューラが非0終了で失敗を検知できるようにする。
        """
        with session_scope() as session:
            run = session.query(PipelineRun).filter_by(run_id=self.run_id).one()
            run.finished_at = datetime.datetime.now(datetime.UTC)
            run.status = "failed"
            run.health = [
                *(run.health or []),
                {
                    "code": "run_unhandled_exception",
                    "severity": "error",
                    "message": (
                        f"パイプラインが工程を包む例外処理の外側で停止しました: "
                        f"{type(exc).__name__}: {exc}"
                    )[:_ERROR_MESSAGE_MAX],
                    "detail": {"exception_type": type(exc).__name__},
                },
            ]
        logger.error(
            "pipeline run failed with unhandled exception: run_id=%s exception=%s",
            self.run_id,
            type(exc).__name__,
            exc_info=exc,
        )

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


def sweep_orphan_runs(
    threshold: datetime.timedelta = DEFAULT_ORPHAN_SWEEP_THRESHOLD,
) -> list[uuid.UUID]:
    """heartbeatが `threshold` 以上進んでいない `running` runを `aborted` に落とす
    (A-2、監査§10.3/10.4)。

    2026-09-03の停止run(gate stageのFK違反でプロセスが異常終了し、
    `pipeline_runs.status` が `running` のまま残った)を、手作業のUPDATEを
    前提にせず自然に回収するための仕組み。`run_daily_pipeline()` の冒頭
    (新しいrunを作る前)と、CLIの `sweep-orphan-runs` コマンドの両方から
    呼べる。

    `last_heartbeat_at` がまだ無い行(A-2導入以前に作られ、マイグレーションで
    `started_at` を初期値として埋めた行を含む)も対象にする——`OR` 条件で
    `last_heartbeat_at IS NULL AND started_at < cutoff` を含めているのはその
    ためで、旧い孤児runがマイグレーション後も回収されずに残り続けることを防ぐ。
    """
    now = datetime.datetime.now(datetime.UTC)
    cutoff = now - threshold
    aborted_run_ids: list[uuid.UUID] = []
    with session_scope() as session:
        stuck_runs = (
            session.query(PipelineRun)
            .filter(PipelineRun.status == "running")
            .filter(
                sa.or_(
                    sa.and_(PipelineRun.last_heartbeat_at.is_(None), PipelineRun.started_at < cutoff),
                    PipelineRun.last_heartbeat_at < cutoff,
                )
            )
            .all()
        )
        for run in stuck_runs:
            last_seen = run.last_heartbeat_at or run.started_at
            run.status = "aborted"
            run.finished_at = now
            run.health = [
                *(run.health or []),
                {
                    "code": "run_orphaned_swept",
                    "severity": "error",
                    "message": (
                        f"heartbeatが{int(threshold.total_seconds() // 60)}分以上"
                        "更新されなかったため、実行を aborted として確定しました"
                        "(プロセスが強制終了した可能性があります)。"
                    ),
                    "detail": {"last_seen_at": last_seen.isoformat() if last_seen else None},
                },
            ]
            aborted_run_ids.append(run.run_id)

    if aborted_run_ids:
        logger.warning(
            "orphan sweep aborted %d stuck pipeline run(s): %s",
            len(aborted_run_ids),
            [str(run_id) for run_id in aborted_run_ids],
        )
    return aborted_run_ids
