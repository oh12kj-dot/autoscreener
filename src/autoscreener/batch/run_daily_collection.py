"""日次データ収集バッチ(6.1)。並列実行・サーキットブレーカー等の共通ロジックは
parallel_runner.py に集約している(18.3・18.4)。"""

from __future__ import annotations

import datetime
import uuid
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session

from autoscreener.batch.parallel_runner import run_parallel
from autoscreener.collectors.snapshot_collector import collect_one
from autoscreener.config import CollectionConfig, load_collection_config
from autoscreener.dates import utc_today
from autoscreener.db.models import Ticker
from autoscreener.db.session import session_scope


def select_collectable_symbols(session: Session, collection_config: CollectionConfig) -> list[str]:
    """その日の収集対象シンボル(18.1)。

    18.1の隔離リストの仕様は「日次の通常対象から除外。**週次で再挑戦し、復旧すれば
    自動復帰**(無限リトライ・無限スキップの両方を回避)」である。ところが呼び出し元
    (`daily_pipeline` と `cli._resolve_symbols`)は `is_quarantined == False` で
    無条件に絞っていたため、**一度隔離された銘柄が二度と収集されなかった**。
    `is_quarantined` は `collect_one` の成功時にしか解除されないので、収集対象から
    外れた銘柄は永久に復旧できない——「無限スキップ」そのものであり、
    `collect_one` にある再挑戦間隔の判定も `quarantine.retry_interval_days` も
    到達不能な死んだコード・死んだ設定値になっていた。

    ここで「再挑戦期限が来た隔離銘柄」を対象に含めることで復帰経路を作る。
    期限が来ていない隔離銘柄をそもそもリストへ入れないのは、`collect_one` が
    返す `quarantined` ステータスが収集成功率(18.7 の監視閾値)の分母を膨らませ、
    健全な実行を「成功率低下」と誤報させないため。`collect_one` 側の同じ判定は
    多層防御として残す(シンボルを直接指定して実行する経路があるため)。
    """
    retry_cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=collection_config.quarantine.retry_interval_days
    )
    rows = (
        session.query(Ticker.symbol)
        .filter(
            # 上場廃止は一時的な隔離ではなく永続的な取引不能状態。隔離の
            # 再挑戦期限が来ても収集対象へ戻してはいけない。
            Ticker.delisted_at.is_(None),
            or_(
                Ticker.is_quarantined.is_(False),
                Ticker.last_attempted_at.is_(None),
                Ticker.last_attempted_at <= retry_cutoff,
            )
        )
        .all()
    )
    return [row[0] for row in rows]


def collection_population_counts(session: Session) -> tuple[int, int]:
    """Return quarantined and total counts for the live collection population.

    Delisted rows are retained for point-in-time validation, but they are not
    candidates for daily collection and must not make quarantine health look
    degraded permanently.
    """
    query = session.query(Ticker)
    quarantined = query.filter(
        Ticker.is_quarantined.is_(True),
        Ticker.delisted_at.is_(None),
    ).count()
    total = query.count()
    delisted = query.filter(Ticker.delisted_at.is_not(None)).count()
    return quarantined, total - delisted


def run_daily_collection(
    symbols: list[str],
    collection_config: CollectionConfig | None = None,
    snapshot_date: date | None = None,
    market_session_date: date | None = None,
    force_statement_refresh: bool = False,
) -> dict[str, int]:
    """候補銘柄リストを収集する。戻り値は状態ごとの件数。"""
    collection_config = collection_config or load_collection_config()
    snapshot_date = snapshot_date or utc_today()

    def worker(symbol: str, run_id: uuid.UUID) -> str:
        with session_scope() as session:
            return collect_one(
                session,
                run_id,
                symbol,
                collection_config,
                snapshot_date,
                market_session_date=market_session_date,
                force_statement_refresh=force_statement_refresh,
            )

    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):広域障害でブレーカーが
    # 作動したら、その実行で積み上がった consecutive_failures を巻き戻して
    # 一斉隔離を防ぐ。
    return run_parallel(
        symbols, worker, collection_config, snapshot_date, rollback_consecutive_failures_on_trip=True
    )
