"""価格・株式数の履歴バックフィル(1回限りのジョブ)。

15.2の流動性ゲート(日次売買代金の中央値)・希薄化ゲート(3年CAGR)を計算する
には数十営業日〜3年分の時系列が要る。日次収集の自然蓄積を待つのではなく、
yfinanceから一括で過去分を取得する(collectors.yfinance_client.
fetch_price_and_shares_history参照)。
"""

from __future__ import annotations

import datetime
import logging
import uuid

from autoscreener.batch.parallel_runner import run_parallel
from autoscreener.collectors.errors import CollectionError
from autoscreener.collectors.snapshot_collector import get_or_create_ticker
from autoscreener.collectors.yfinance_client import fetch_price_and_shares_history
from autoscreener.config import CollectionConfig, load_collection_config
from autoscreener.dates import utc_today
from autoscreener.db.models import CollectionLog, PriceSnapshot
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)


def _backfill_one(
    session,
    run_id: uuid.UUID,
    symbol: str,
    collection_config: CollectionConfig,
    run_date: datetime.date,
    period: str = "max",
) -> str:
    ticker = get_or_create_ticker(session, symbol)

    try:
        # B-4(docs/defect_and_edge_audit_2026-08-28.md I-1):既定を "3y" から "max" へ。
        # companyfacts が2009年まで遡れても、価格が3年しか無ければバックテストの
        # 評価日を増やせない。
        rows = fetch_price_and_shares_history(symbol, collection_config.retry, period=period)
    except CollectionError as exc:
        session.add(
            CollectionLog(
                run_id=run_id,
                ticker_id=ticker.id,
                snapshot_date=run_date,
                status="backfill_failed",
                detail={"error": str(exc)},
            )
        )
        return "backfill_failed"

    if not rows:
        session.add(
            CollectionLog(
                run_id=run_id, ticker_id=ticker.id, snapshot_date=run_date, status="backfill_empty", detail=None
            )
        )
        return "backfill_empty"

    # 既存行を1クエリで取得し、銘柄あたり最大750行(3年分)を1行ずつ
    # 存在確認しない(N+1回避)。
    existing_by_date = {
        row.trade_date: row
        for row in session.query(PriceSnapshot).filter(PriceSnapshot.ticker_id == ticker.id).all()
    }

    inserted = 0
    updated = 0
    for row in rows:
        existing = existing_by_date.get(row["trade_date"])
        if existing is None:
            session.add(PriceSnapshot(ticker_id=ticker.id, **row))
            inserted += 1
            continue
        # 既存行は「取得当時の単位」のまま残っている可能性がある(13.4:日次収集で
        # 積み上げた行は、その後の株式分割で単位がずれる)。バックフィルの取得値は
        # 常に現時点で分割調整済みなので、上書きして収束させる(18.3:再実行すれば
        # 正しい値に収束することもべき等性に含まれる)。以前は既存日付を単に
        # スキップしていたため、再バックフィルしても古い値が直らなかった。
        changed = False
        for field, value in row.items():
            if field == "trade_date":
                continue
            current = getattr(existing, field)
            if current is None and value is None:
                continue
            if current is None or value is None or float(current) != float(value):
                setattr(existing, field, value)
                changed = True
        if changed:
            updated += 1

    session.add(
        CollectionLog(
            run_id=run_id,
            ticker_id=ticker.id,
            snapshot_date=run_date,
            status="backfill_success",
            detail={"fetched": len(rows), "inserted": inserted, "updated": updated},
        )
    )
    return "backfill_success"


def backfill_history(
    symbols: list[str],
    collection_config: CollectionConfig | None = None,
    period: str = "max",
) -> dict[str, int]:
    collection_config = collection_config or load_collection_config()
    run_date = utc_today()

    def worker(symbol: str, run_id: uuid.UUID) -> str:
        with session_scope() as session:
            return _backfill_one(session, run_id, symbol, collection_config, run_date, period=period)

    return run_parallel(
        symbols,
        worker,
        collection_config,
        run_date,
        failure_statuses={"backfill_failed"},
    )
