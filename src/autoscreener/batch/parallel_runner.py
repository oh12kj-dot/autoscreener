"""複数銘柄への並列実行の共通ロジック(8.1・8.3・14.9・18.3・18.4)。

日次収集(run_daily_collection)と履歴バックフィル(backfill_history)の両方が
同じ「並列実行・ジッタ・サーキットブレーカー」の仕組みを必要とするため、
ここに1箇所へ集約する(重複実装を避ける)。
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from sqlalchemy.orm import Session

from autoscreener.collectors.rate_limit import configure_shared_limiter
from autoscreener.config import CollectionConfig
from autoscreener.db.models import CollectionLog, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

# サーキットブレーカーの失敗率計算に含める状態。permanent_failure は「想定内の
# 恒久除外」であり、Yahoo側の広域障害を示すシグナルではないため含めない(18.1)。
#
# `empty_response_delisted`(B-5)は `empty_response` が長期間続いた結果に過ぎず、
# 障害シグナルとしての性質は `empty_response` と同じなので同列に数える。
# 除くと、広域障害が長引いたときにブレーカーの感度が静かに下がる。
#
# `unhandled_error`(E-3、2026-08-27)は worker_fn 側の分類外の例外(DB接続断・
# NULL参照等のコードバグ)であり、全銘柄で発生しても現状ブレーカーが作動せず、
# 実際には全滅している実行が最後まで走り続けていた。想定内の恒久除外である
# permanent_failure とは性質が違うため、広域異常のシグナルとして数える。
DEFAULT_FAILURE_STATUSES = {
    "transient_failure",
    "empty_response",
    "empty_response_delisted",
    "parse_failure",
    "unhandled_error",
}


def _rollback_run_failures(
    run_id: uuid.UUID,
    snapshot_date: date,
    failure_statuses: set[str],
    consecutive_failure_threshold: int,
) -> int:
    """A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):一斉隔離を障害として扱う。

    サーキットブレーカーが作動した実行は、1銘柄ずつの恒久的失敗ではなく
    インフラ側の広域障害である可能性が高い。その実行で積み上がった
    `consecutive_failures` の増分を巻き戻し、この実行のせいで閾値を越えて
    隔離された銘柄を解放する。1銘柄の恒久的失敗と全滅は別の事象であり、
    後者で数千銘柄を一斉に隔離すると復旧が `retry_interval_days` 遅延する。
    """
    with session_scope() as session:
        failed_ticker_ids = [
            row[0]
            for row in session.query(CollectionLog.ticker_id)
            .filter(
                CollectionLog.run_id == run_id,
                CollectionLog.ticker_id.isnot(None),
                CollectionLog.status.in_(failure_statuses),
            )
            .all()
        ]
        if not failed_ticker_ids:
            return 0
        tickers = session.query(Ticker).filter(Ticker.id.in_(failed_ticker_ids)).all()
        for ticker in tickers:
            ticker.consecutive_failures = max(0, ticker.consecutive_failures - 1)
            if ticker.is_quarantined and ticker.consecutive_failures < consecutive_failure_threshold:
                ticker.is_quarantined = False
        session.add(
            CollectionLog(
                run_id=run_id,
                ticker_id=None,
                snapshot_date=snapshot_date,
                status="failures_rolled_back",
                detail={"ticker_count": len(tickers)},
            )
        )
        return len(tickers)


def run_parallel(
    symbols: list[str],
    worker_fn: Callable[[str, uuid.UUID], str],
    collection_config: CollectionConfig,
    snapshot_date: date,
    failure_statuses: set[str] = DEFAULT_FAILURE_STATUSES,
    rollback_consecutive_failures_on_trip: bool = False,
) -> dict[str, int]:
    """symbols に対して worker_fn(symbol, run_id) -> status を並列実行する。

    - ジッタで規則的なリクエストパターンを避ける(8.3)
    - サーキットブレーカー:min_sample_size件処理後、失敗率が閾値を超えたら
      未着手分をキャンセルして中断する(18.4・18.7)
    - `rollback_consecutive_failures_on_trip`:ブレーカー作動時、その実行で
      積み上がった `consecutive_failures` を巻き戻し一斉隔離を防ぐ(A-1 / D-12)。
      `worker_fn` が `consecutive_failures` を進める収集経路でのみ True にする。
    """
    run_id = uuid.uuid4()
    logger.info("parallel run %s starting: %d symbols", run_id, len(symbols))

    # 設定した秒あたり上限を、Yahoo向けの共有リミッターに反映する。
    # **ここが設定を読む唯一の場所**なので、ここで入れておかないと
    # `yfinance_client` は安全側の既定値のまま動く(遅すぎて気づきにくい)。
    configure_shared_limiter("yfinance", collection_config.yfinance_requests_per_second)

    # B-6(2026-08-26、docs/model_audit_v4_2026-08-26.md):`/universe/status` が実行
    # 完了までの途中経過をそのまま返しており、「1,215銘柄しか取れていない」と
    # 誤読される原因になっていた。開始時の対象件数を記録しておき、APIが
    # `sum(status_counts) / target_count` で進捗率を算出できるようにする。
    with session_scope() as session:
        session.add(
            CollectionLog(
                run_id=run_id,
                ticker_id=None,
                snapshot_date=snapshot_date,
                status="run_started",
                detail={"target_count": len(symbols)},
            )
        )

    def _with_jitter(symbol: str) -> str:
        time.sleep(
            random.uniform(
                collection_config.request_jitter_min_seconds, collection_config.request_jitter_max_seconds
            )
        )
        return worker_fn(symbol, run_id)

    processed = 0
    failures = 0
    status_counts: dict[str, int] = {}
    circuit_breaker_tripped = False

    with ThreadPoolExecutor(max_workers=collection_config.max_workers) as executor:
        futures = {executor.submit(_with_jitter, symbol): symbol for symbol in symbols}

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                status = future.result()
            except Exception:
                logger.exception("unhandled error processing %s", symbol)
                status = "unhandled_error"

            status_counts[status] = status_counts.get(status, 0) + 1
            processed += 1
            if status in failure_statuses:
                failures += 1

            if (
                not circuit_breaker_tripped
                and processed >= collection_config.circuit_breaker.min_sample_size
                and failures / processed >= collection_config.circuit_breaker.failure_rate_threshold
            ):
                circuit_breaker_tripped = True
                logger.error(
                    "circuit breaker tripped: %d/%d failed (>= %.0f%%), cancelling remaining work",
                    failures,
                    processed,
                    collection_config.circuit_breaker.failure_rate_threshold * 100,
                )
                with session_scope() as session:
                    session.add(
                        CollectionLog(
                            run_id=run_id,
                            ticker_id=None,
                            snapshot_date=snapshot_date,
                            status="circuit_breaker_tripped",
                            detail={"processed": processed, "failures": failures},
                        )
                    )
                executor.shutdown(wait=False, cancel_futures=True)
                break

    if circuit_breaker_tripped and rollback_consecutive_failures_on_trip:
        rolled_back = _rollback_run_failures(
            run_id,
            snapshot_date,
            failure_statuses,
            collection_config.quarantine.consecutive_failure_threshold,
        )
        logger.error(
            "circuit breaker rollback: reverted consecutive_failures for %d tickers "
            "(treating the trip as an infrastructure outage, not per-ticker failure) — A-1/D-12",
            rolled_back,
        )
        status_counts["failures_rolled_back"] = rolled_back

    logger.info("parallel run %s finished: %s", run_id, status_counts)
    with session_scope() as session:
        session.add(
            CollectionLog(
                run_id=run_id,
                ticker_id=None,
                snapshot_date=snapshot_date,
                status="run_finished",
                detail={"processed": processed, "circuit_breaker_tripped": circuit_breaker_tripped},
            )
        )
    return status_counts


def run_parallel_tickers(
    ticker_ids: list[int],
    worker_fn: Callable[[Session, int], dict[str, int]],
    max_workers: int,
) -> dict[str, int]:
    """EDGAR系工程(litigation/filing_sections/dilution/customer_concentration)
    向けの軽量並列実行(S-5、docs/daily_pipeline_throughput_plan_2026-09-04.md)。

    `run_parallel`(yfinance収集向け、`CollectionConfig`のジッタ・サーキット
    ブレーカーを要する)とは別に用意する。EDGAR側は共有`sec`リミッター
    (`collectors/rate_limit.py`)が既にHTTPリクエスト単位で秒間上限を守って
    おり、ここが遅かったのはレート制限ではなく**単に並列度がゼロ
    (完全な逐次ループ)だったから**(実測:litigationは299銘柄で実質
    0.26 req/秒、設定上限5.0の5%しか使えていなかった)。余っている枠を
    並列化で使い切るだけなので、上限(`edgar.requests_per_second`)は
    触らない。ジッタもサーキットブレーカーも、SEC側の障害検知は
    `EdgarClient._apply_server_backoff`(429/503のRetry-After・403の遮断で
    全体を止める)が既に別の層で持っているため、ここでは不要。

    SQLAlchemyの`Session`はスレッドセーフではないため、銘柄ごとに専用の
    `session_scope()`を開く(`run_daily_collection.py`のworkerと同じ理由)。
    `worker_fn`は`(session, ticker_id)`を受け取り、その銘柄ぶんの件数辞書
    (例:`{"tickers": 1, "new_events": 2, "failures": 0}`)を返す——
    呼び出し元がキー単位で合算する。
    """
    totals: dict[str, int] = {}
    totals_lock = threading.Lock()

    def _run(ticker_id: int) -> None:
        with session_scope() as session:
            result = worker_fn(session, ticker_id)
        with totals_lock:
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_run, ticker_id) for ticker_id in ticker_ids]
        for future in as_completed(futures):
            future.result()  # ワーカー内の想定外の例外はここで再送出する

    return totals
