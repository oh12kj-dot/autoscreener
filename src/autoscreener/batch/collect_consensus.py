"""Append-only, content-deduplicated consensus collection."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from autoscreener.collectors.consensus import ConsensusProvider, YfinanceConsensusProvider
from autoscreener.collectors.rate_limit import configure_shared_limiter
from autoscreener.config import CollectionConfig, load_collection_config
from autoscreener.db.models import AnalystConsensusSnapshot, RawSnapshot, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

_EMPTY_STATS = {"processed": 0, "inserted": 0, "unchanged": 0, "failed": 0, "no_finding": 0}


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _latest_target_mean_price(session, ticker_id: int) -> float | None:
    """S-3(daily_pipeline_throughput_plan_2026-09-04):consensusの`.info`取得
    (2本目のHTTP)を廃止する代わりに、collection工程が数時間前に保存した
    `raw_snapshots.payload["info"]`から`targetMeanPrice`を読む。最新1件で
    十分——アナリスト予想は表示専用でゲート・スコアに入らないため、
    `apply_gates.py`のような`available_from`基準の厳密なPIT切り出しは
    要らない(`docs/daily_pipeline_throughput_plan_2026-09-04.md` S-3)。
    """
    raw = (
        session.query(RawSnapshot)
        .filter(RawSnapshot.ticker_id == ticker_id)
        .order_by(RawSnapshot.snapshot_date.desc())
        .first()
    )
    if raw is None:
        return None
    info = raw.payload.get("info") or {}
    value = info.get("targetMeanPrice")
    return float(value) if isinstance(value, (int, float)) else None


def _process_ticker(
    ticker_id: int, symbol: str, provider: ConsensusProvider, as_of: datetime.datetime
) -> dict[str, int]:
    """1銘柄ぶんの収集を専用セッションで行う(S-4:並列化のため銘柄ごとに
    独立したセッションを使う——SQLAlchemyのSessionはスレッドセーフではない、
    `batch/run_daily_collection.py`のworkerと同じ理由)。"""
    stats = dict(_EMPTY_STATS)
    stats["processed"] = 1
    with session_scope() as session:
        try:
            inserted = unchanged = no_finding = 0
            # 一つのticker専用セッション内でも、DB制約違反等でflushが失敗した
            # ときに「失敗ログ行を書く」ための後続INSERTまでトランザクションを
            # 巻き込まないよう、以前と同じくSAVEPOINTで区切る。
            with session.begin_nested():
                target_mean_price = _latest_target_mean_price(session, ticker_id)
                snapshots = provider.fetch(symbol, as_of, target_mean_price)
                if not snapshots:
                    no_finding = 1
                    payload = {"coverage_status": "collected_no_finding"}
                    digest = _hash(payload)
                    latest = session.query(AnalystConsensusSnapshot).filter_by(
                        ticker_id=ticker_id, source=provider.name
                    ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                    if latest and latest.content_hash == digest:
                        unchanged += 1
                    else:
                        session.add(AnalystConsensusSnapshot(
                            ticker_id=ticker_id, observed_at=as_of, source=provider.name,
                            period_type="NA", period_end=None, raw_payload=payload,
                            coverage_status="collected_no_finding", confidence="medium", content_hash=digest,
                        ))
                        inserted += 1
                else:
                    seen: dict[tuple[str, datetime.date | None], str] = {}
                    for snap in snapshots:
                        payload = {k: v for k, v in vars(snap).items() if k != "observed_at"}
                        digest = _hash(payload)
                        key = (snap.source, snap.period_end)
                        if key in seen:
                            if seen[key] == digest:
                                unchanged += 1
                                continue
                            raise ValueError(
                                "provider returned conflicting consensus rows "
                                f"for source={snap.source} period_end={snap.period_end}"
                            )
                        seen[key] = digest
                        latest = session.query(AnalystConsensusSnapshot).filter_by(
                            ticker_id=ticker_id, source=snap.source, period_end=snap.period_end
                        ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                        if latest and latest.content_hash == digest:
                            unchanged += 1
                            continue
                        session.add(AnalystConsensusSnapshot(
                            ticker_id=ticker_id, observed_at=snap.observed_at, source=snap.source,
                            source_url=snap.source_url, period_type=snap.period_type, period_end=snap.period_end,
                            revenue_mean=snap.revenue_mean, revenue_low=snap.revenue_low,
                            revenue_high=snap.revenue_high, eps_mean=snap.eps_mean,
                            ebitda_mean=snap.ebitda_mean, analyst_count=snap.analyst_count,
                            target_price_mean=snap.target_price_mean, raw_payload=snap.raw_payload,
                            coverage_status="collected_with_data", confidence="medium", content_hash=digest,
                        ))
                        inserted += 1
            stats["inserted"] = inserted
            stats["unchanged"] = unchanged
            stats["no_finding"] = no_finding
        except Exception as exc:
            stats["failed"] = 1
            payload = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
            digest = _hash(payload)
            with session.begin_nested():
                latest = session.query(AnalystConsensusSnapshot).filter_by(
                    ticker_id=ticker_id, source=provider.name
                ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                if latest is None or latest.content_hash != digest:
                    session.add(AnalystConsensusSnapshot(
                        ticker_id=ticker_id, observed_at=as_of, source=provider.name,
                        period_type="NA", period_end=None, raw_payload=payload,
                        coverage_status="collection_failed", confidence="low", content_hash=digest,
                    ))
    return stats


def collect_consensus(
    provider: ConsensusProvider | None = None,
    *,
    as_of: datetime.datetime | None = None,
    symbols: list[str] | None = None,
    collection_config: CollectionConfig | None = None,
) -> dict[str, int]:
    """S-4(daily_pipeline_throughput_plan_2026-09-04):以前は
    `for ticker in query.all():`の完全な逐次ループで、`run_parallel`も
    使っておらず共有リミッターも通っていなかった(実測:5,889銘柄/6,732秒=
    0.875銘柄/秒、実効HTTPは1.75 req/秒程度)。逐次だから遅かっただけで、
    レート制限が効いていたわけではない。

    S-1(リミッターのHTTP単位化)とS-3(`.info`廃止)の後であれば、共有
    `yfinance`リミッター配下で並列化しても6.0 req/秒の天井を超えない
    ——ただしこれは`YfData._make_request`(実HTTP境界)がスロットル
    済みであることが前提であり、**その前提はこのモジュール自身の
    import グラフによって保証しなければならない**。

    2026-09-04監査で発見:`_install_http_throttle()`(S-1)は
    `collectors/yfinance_client`が**importされた時点で**実行される
    副作用であって、プロセス起動時に自動的に走る魔法ではない。ところが
    本モジュールは`collectors.consensus`しかimportしておらず、当の
    `collectors.consensus`も以前は`yfinance_client`を一切importして
    いなかった。実運用(`cli.py`→`snapshot_collector`→`yfinance_client`の
    別経路)で偶然スロットルが入っていただけで、`collect_consensus()`を
    直接呼ぶ別の呼び出し経路(スクリプト・将来のCLI配線・単体テスト)
    からは無制限にYahooへ投げる状態が起こりえた。修正:
    `collectors/consensus.py`が自分自身のモジュール直下で`yfinance_client`
    をimportするよう変更し(同ファイル冒頭のコメント参照)、
    「`collectors.consensus`(=本モジュールの直接の依存)がimportされれば
    必ずスロットルが入る」ことをimport順序に依存しない構造的な保証にした。

    ここでの並列化は「上限を触らず、余っている枠を使い切るだけ」であり、
    collectionと同じ`max_workers`をそのまま使う(計画書6章:速くする
    名目で上限を引き上げない)。
    """
    provider = provider or YfinanceConsensusProvider()
    as_of = as_of or datetime.datetime.now(datetime.timezone.utc)
    collection_config = collection_config or load_collection_config()
    # **ここが設定を読む唯一の場所ではない**(`run_daily_collection`経由の
    # collection工程も読む)が、consensusが単独で(collectionより先に、
    # または別プロセスで)呼ばれても安全側の既定(2.0 req/秒)に落ちずに
    # 正しい上限で動くよう、ここでも反映しておく。
    configure_shared_limiter("yfinance", collection_config.yfinance_requests_per_second)

    with session_scope() as session:
        query = session.query(Ticker.id, Ticker.symbol).filter(Ticker.is_benchmark.is_(False))
        if symbols:
            query = query.filter(Ticker.symbol.in_([s.upper() for s in symbols]))
        ticker_rows = query.all()

    stats = dict(_EMPTY_STATS)
    stats_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=collection_config.max_workers) as executor:
        futures = {
            executor.submit(_process_ticker, ticker_id, symbol, provider, as_of): symbol
            for ticker_id, symbol in ticker_rows
        }
        for future in as_completed(futures):
            result = future.result()
            with stats_lock:
                for key, value in result.items():
                    stats[key] += value

    return stats
