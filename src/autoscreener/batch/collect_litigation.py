"""訴訟・SEC調査・ショートレポートの収集バッチ(K-5)。

デューデリ11工程のうち「訴訟・ショートレポート」だけが Google 検索リンクしか
無く、完全に人間の手作業として残っていた工程の機械版。

**10-K Item 3(訴訟)本文が `filing_sections` にあれば正規表現抽出を優先し、
EDGAR全文検索APIは補助にする**(`collectors/litigation_source.py` のdocstring
参照)。SECへのリクエスト数を減らすため、Item 3 から検出できた種別は全文検索を
省略し、検出できなかった種別(典型的にはshort_report——ショートセラーレポートは
Item 3 の訴訟開示ではなく8-Kで語られることが多い)だけを全文検索で補う。

原則3:このバッチが書く `litigation_events` は `evaluate_gates` にも
`scoring/` にも一切読まれない。表示・チェックリスト・アラートのみが読者。

例外は銘柄単位で握ってログに残し、次の銘柄へ進む。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.batch.parallel_runner import run_parallel_tickers
from autoscreener.collectors.litigation_source import (
    LITIGATION_QUERY_PHRASES,
    EdgarFullTextSearchClient,
    LitigationHit,
    detect_litigation_mentions,
    fetch_litigation,
)
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.dates import utc_today
from autoscreener.db.models import FilingSection, LitigationEvent, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 300
_ITEM3_LOOKBACK_ROWS = 4  # 直近数件(おおむね3年分)のItem3を見れば十分

LitigationFetcher = Callable[[Ticker, "frozenset[str]"], list[LitigationHit]]


def _upsert(
    session: Session,
    ticker_id: int,
    *,
    kind: str,
    title: str,
    event_date: datetime.date,
    detail: str | None,
    source_url: str | None,
    source_accession: str | None,
    collected_on: datetime.date,
) -> bool:
    """`(ticker_id, kind, event_date, title)` が既存なら何もしない(モデルの
    UniqueConstraint と同じキー)。戻り値は新規作成したか。"""
    exists = (
        session.query(LitigationEvent)
        .filter_by(ticker_id=ticker_id, kind=kind, event_date=event_date, title=title)
        .first()
    )
    if exists is not None:
        return False
    session.add(
        LitigationEvent(
            ticker_id=ticker_id,
            event_date=event_date,
            kind=kind,
            title=title,
            detail=detail,
            source_url=source_url,
            source_accession=source_accession,
            collected_on=collected_on,
        )
    )
    return True


def _collect_from_item3(
    session: Session, ticker: Ticker, *, collected_on: datetime.date, counts: dict[str, int]
) -> set[str]:
    """`filing_sections`(section='item3')から正規表現で訴訟言及を拾う。

    戻り値は検出できた種別の集合(全文検索を補助的に呼ぶかどうかの判定に使う)。
    """
    found_kinds: set[str] = set()
    sections = (
        session.query(FilingSection)
        .filter(FilingSection.ticker_id == ticker.id, FilingSection.section == "item3")
        .order_by(FilingSection.filed_date.desc())
        .limit(_ITEM3_LOOKBACK_ROWS)
        .all()
    )
    for section in sections:
        for mention in detect_litigation_mentions(section.text):
            found_kinds.add(mention.kind)
            created = _upsert(
                session,
                ticker.id,
                kind=mention.kind,
                title=f"10-K Item 3: {mention.kind}",
                event_date=section.filed_date,
                detail=mention.evidence,
                source_url=section.source_url,
                source_accession=section.accession_number,
                collected_on=collected_on,
            )
            counts["new_events" if created else "existing"] += 1
    return found_kinds


def _default_fetcher() -> LitigationFetcher | None:
    """既定の全文検索フェッチャーを組み立てる。`EDGAR_USER_AGENT` 未設定なら
    無効(全文検索なしでItem3経路のみ動作させる)。"""
    settings = get_settings()
    if not settings.edgar_user_agent:
        logger.info("EDGAR_USER_AGENT is not set; full text search fallback disabled")
        return None
    try:
        client = EdgarFullTextSearchClient(settings.edgar_user_agent)
    except ValueError:
        logger.warning("failed to build EdgarFullTextSearchClient", exc_info=True)
        return None

    def _fetch(ticker: Ticker, kinds: frozenset[str]) -> list[LitigationHit]:
        if not ticker.cik:
            return []
        return fetch_litigation(client, ticker.symbol, ticker.cik, kinds=kinds)

    return _fetch


def _process_ticker(
    session: Session, ticker_id: int, active_fetcher: LitigationFetcher | None
) -> dict[str, int]:
    """1銘柄ぶんの訴訟収集を専用セッションで行う(S-5:並列化のため銘柄ごとに
    独立したセッションを使う——SQLAlchemyのSessionはスレッドセーフではない、
    `batch/run_daily_collection.py`のworkerと同じ理由)。"""
    local = {"tickers": 1, "new_events": 0, "existing": 0, "failures": 0}
    ticker = session.get(Ticker, ticker_id)
    if ticker is None:
        return local
    try:
        found_kinds = _collect_from_item3(session, ticker, collected_on=utc_today(), counts=local)

        missing_kinds = frozenset(LITIGATION_QUERY_PHRASES) - found_kinds
        if missing_kinds and active_fetcher is not None:
            hits = active_fetcher(ticker, missing_kinds)
            for hit in hits:
                created = _upsert(
                    session,
                    ticker.id,
                    kind=hit.kind,
                    title=hit.title,
                    event_date=hit.event_date,
                    detail=hit.detail,
                    source_url=hit.source_url,
                    source_accession=hit.source_accession,
                    collected_on=utc_today(),
                )
                local["new_events" if created else "existing"] += 1
    except Exception:
        logger.exception("%s: litigation collection failed", ticker.symbol)
        local["failures"] += 1
    return local


def collect_litigation(
    symbols: list[str] | None = None,
    *,
    limit: int = _DEFAULT_LIMIT,
    fetcher: LitigationFetcher | None = None,
    max_workers: int | None = None,
) -> dict[str, int]:
    """追跡対象銘柄の訴訟・SEC調査・ショートレポートを収集し `litigation_events`
    へ upsert する。

    `fetcher` はテストでネットワークに出ないように差し替え可能(既定は
    `_default_fetcher()` が組み立てるEDGAR全文検索クライアント。
    `EDGAR_USER_AGENT` 未設定ならItem3経路のみで動作する)。

    S-5(daily_pipeline_throughput_plan_2026-09-04):以前は
    `for ticker in tickers:` の完全な逐次ループだった(実測:299銘柄で
    実質0.26 req/秒、設定上限5.0の5%)。銘柄ごとに独立したセッションを
    開いて共有`sec`リミッター配下で並列化する——上限自体は動かさない。

    戻り値は {"tickers": n, "new_events": n, "existing": n, "failures": n}。
    """
    config = load_edgar_config()
    active_fetcher = fetcher if fetcher is not None else (_default_fetcher() if config.enabled else None)

    with session_scope() as session:
        if symbols:
            ticker_ids = [
                row[0]
                for row in session.query(Ticker.id).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
            ]
        else:
            ticker_ids = [t.id for t in select_tracked_tickers(session, limit=limit)]

    counts = run_parallel_tickers(
        ticker_ids,
        lambda session, ticker_id: _process_ticker(session, ticker_id, active_fetcher),
        max_workers=max_workers or config.max_workers,
    )
    zeros = {"tickers": 0, "new_events": 0, "existing": 0, "failures": 0}
    return {**zeros, **counts}
