"""A-5(2026-09-04、docs/racr_wp_a_operational_safety_2026-09-04.md、
監査§10.3「`/ready` はDBと最新V4 scoreがあればreadyとなり、pipeline
failure/stalenessを表さない」・§10.4修正案7):`/operational-readiness` の
判定本体。

**`/ready`(`api/main.py`)の契約は変えない。** `/ready` は「このプロセスが
DBと設定ファイルに噛み合っているか」だけを見る、既存の意味のまま残す
(28.17の教訓:v4移行時に `/ready` がDBしか見ておらず、設定スキーマ不一致で
全エンドポイントが500を返しているのに200を返し続けた——「readyと言って
いるのに何も動かない」という、切り分けに最も使えない状態だった)。

ここで新設する `/operational-readiness` は別の問いに答える:「日次パイプラインは
実際に回っていて、ランキングの元データは新しいか」。両者は独立した問いであり、
同じエンドポイントへ混ぜると28.17と同じ失敗を形を変えて再発させる——
「プロセスは健全だがデータが古い」と「プロセス自体が壊れている」は、
利用者が取るべき行動(前者は日次パイプラインを確認、後者はAPIプロセスを
再起動)が違う。

このエンドポイントは常に200を返す(`/ready` のような可用性プローブでは
なく、運用ダッシュボードが読む状態レポートのため)。`status` フィールドが
`"ready"` / `"degraded"` を表す。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import func, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from autoscreener.batch.pipeline_recorder import DEFAULT_ORPHAN_SWEEP_THRESHOLD
from autoscreener.config import PROJECT_ROOT
from autoscreener.db.models import ModelRun, PipelineRun, RawSnapshot, Score, UniverseSnapshot

# A-5:pipelineが生成するデータセットのうち、鮮度を見る対象。日次で更新
# されるはずのもの(universe_snapshots/scores/raw_snapshots/model_scores)に
# 絞る——週次専用工程(filings/macro等)は対象外の曜日があり、単純な
# 「N日以上古い」判定に馴染まないため、監査§10.4修正案7が挙げた4種類だけを見る。
#
# 4日という閾値:日次更新のはずのデータが金曜更新のまま月曜に確認しても
# 3日差になる(土日を挟むため)。それを異常と誤検知しないよう、1日分の
# 余裕を追加した4日を「本当に止まっている」とみなす下限にする。
_STALE_AFTER_CALENDAR_DAYS = 4


@dataclass(frozen=True)
class DatasetFreshness:
    dataset: str
    latest_date: datetime.date | None
    # 経過暦日数。None は「データが1件も存在しない」(0日ではない——
    # 「測れなかった」と「今日時点で最新だった」を混同しない、18.7と同じ判断)。
    days_stale: int | None

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "latest_date": self.latest_date.isoformat() if self.latest_date else None,
            "days_stale": self.days_stale,
        }


def _freshness(dataset: str, latest_date: datetime.date | None, today: datetime.date) -> DatasetFreshness:
    if latest_date is None:
        return DatasetFreshness(dataset=dataset, latest_date=None, days_stale=None)
    return DatasetFreshness(dataset=dataset, latest_date=latest_date, days_stale=(today - latest_date).days)


def _alembic_head_revision() -> str | None:
    """コード(`alembic/versions/*.py`)が期待するheadリビジョン。

    読めない場合(パッケージ配置の都合、alembic.iniが見つからない等)は
    `None` を返す——「不一致」と断定せず「判定不能」として扱う(呼び出し側で
    `match: null` になる)。誤って `alembic_head_mismatch` を報告し、実際には
    比較不能なだけの状態を運用者に誤解させないため。
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception:
        return None


def _alembic_db_revision(session: Session) -> str | None:
    """DBが実際に到達しているリビジョン。`alembic_version` テーブルが無い
    (一度もmigrateしていない)場合は `None`。"""
    try:
        return session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except ProgrammingError:
        return None


def build_operational_readiness(session: Session, *, today: datetime.date | None = None) -> dict:
    """`/operational-readiness` の応答本体を組み立てる。

    ルーティング層(`api/main.py`)から呼ばれる。DBセッションだけを受け取る形に
    してあるのは、HTTP経由(`TestClient`)を介さずユニットテストから直接呼べる
    ようにするため——`/ready` は判定項目が少ないためルート関数へ直接書いて
    あるが、こちらは判定項目が多く、`main.py` を肥大させたくない。
    """
    today = today or datetime.datetime.now(datetime.UTC).date()
    reasons: list[str] = []

    # 1. 最新pipeline runのterminal statusと経過時間。
    latest_run = session.query(PipelineRun).order_by(PipelineRun.started_at.desc()).first()
    pipeline_status: dict[str, object] = {"has_run": latest_run is not None}
    if latest_run is None:
        # 監査冒頭0.8「現在は昇格判定不能」と同種の状態:一度もpipelineが
        # 記録されていない(新規導入直後、またはDBが空)。
        reasons.append("no_pipeline_run_recorded")
    else:
        seconds_since_heartbeat: float | None = None
        last_seen = latest_run.last_heartbeat_at or latest_run.started_at
        if last_seen is not None:
            reference = last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=datetime.UTC)
            seconds_since_heartbeat = (datetime.datetime.now(datetime.UTC) - reference).total_seconds()

        pipeline_status.update(
            {
                "run_id": str(latest_run.run_id),
                "run_date": latest_run.run_date.isoformat(),
                "status": latest_run.status,
                "started_at": latest_run.started_at.isoformat(),
                "finished_at": latest_run.finished_at.isoformat() if latest_run.finished_at else None,
                "seconds_since_last_heartbeat": seconds_since_heartbeat,
            }
        )
        if latest_run.status == "running":
            # A-2のsweeper(`sweep_orphan_runs`)がまだ回収していない、
            # heartbeatが止まったrunをここでも検知する——sweeperはpipeline
            # 起動時とCLIからしか呼ばれないため、その間の時間帯に
            # operational-readinessを見た運用者へは、このチェックが唯一の
            # シグナルになりうる。
            stuck = (
                seconds_since_heartbeat is not None
                and seconds_since_heartbeat > DEFAULT_ORPHAN_SWEEP_THRESHOLD.total_seconds()
            )
            if stuck:
                reasons.append("latest_run_stuck_running")
        elif latest_run.status in ("failed", "aborted"):
            reasons.append(f"latest_run_{latest_run.status}")
        elif latest_run.status == "degraded":
            reasons.append("latest_run_degraded")

    # 2. 主要datasetの鮮度(監査§10.4修正案7:score/raw/universe freshness)。
    latest_universe_date = session.query(func.max(UniverseSnapshot.snapshot_date)).scalar()
    latest_score_date = session.query(func.max(Score.score_date)).scalar()
    latest_raw_date = session.query(func.max(RawSnapshot.snapshot_date)).scalar()
    latest_model_score_date = (
        session.query(func.max(ModelRun.as_of)).filter(ModelRun.model_version == "v5").scalar()
    )

    freshness = [
        _freshness("universe_snapshots", latest_universe_date, today),
        _freshness("scores", latest_score_date, today),
        _freshness("raw_snapshots", latest_raw_date, today),
        _freshness("model_scores", latest_model_score_date, today),
    ]
    for item in freshness:
        if item.days_stale is None:
            reasons.append(f"{item.dataset}_never_populated")
        elif item.days_stale > _STALE_AFTER_CALENDAR_DAYS:
            reasons.append(f"{item.dataset}_stale")

    # 3. alembic head一致(監査§10.4修正案7:migration一致)。
    code_head = _alembic_head_revision()
    db_head = _alembic_db_revision(session)
    if code_head is None or db_head is None:
        alembic_match: bool | None = None
    else:
        alembic_match = code_head == db_head
        if not alembic_match:
            reasons.append("alembic_head_mismatch")

    return {
        "status": "degraded" if reasons else "ready",
        "reasons": reasons,
        "pipeline": pipeline_status,
        "dataset_freshness": [item.to_dict() for item in freshness],
        "alembic": {"code_head": code_head, "db_head": db_head, "match": alembic_match},
        "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
