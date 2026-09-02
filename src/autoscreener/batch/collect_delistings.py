"""上場廃止ユニバースの構築バッチ(docs/defect_and_edge_audit_2026-08-28.md D-1 / I-2)。

`collectors.delisting_source` の薄いオーケストレーション。EDGAR フルインデックスを
走査 → 上場廃止イベントを CIK でティッカーへ解決 → `tickers.delisted_at` に登録。

**段階2(バックテスト母集団への投入)は I-1(XBRL ポイントインタイム)と一体で
実装する。** 廃止銘柄には raw_snapshot が無いため、`build_moic_inputs` の入力を
XBRL companyfacts から作る経路(`scoring.point_in_time_xbrl`)が要る。
"""

from __future__ import annotations

import datetime
import logging

from autoscreener.db.models import DelistingEvent, Ticker
from autoscreener.db.session import session_scope

from autoscreener.collectors.delisting_source import (
    collect_delisting_events,
    last_trade_after_delisting,
    register_delisting_events,
)

logger = logging.getLogger(__name__)

# 価格履歴の開始(現状 2023-08)より1年前を既定の走査開始点にする(D-1 推奨)。
DEFAULT_SCAN_START = datetime.date(2022, 8, 1)


def collect_delistings(
    start: datetime.date | None = None, end: datetime.date | None = None
) -> dict[str, int]:
    events = collect_delisting_events(start or DEFAULT_SCAN_START, end)
    logger.info("collected %d delisting filings", len(events))
    counts = register_delisting_events(events)
    counts["events"] = len(events)
    return counts


def rollback_false_delistings(*, apply: bool = False, symbols: list[str] | None = None) -> dict[str, int]:
    """D-1 の上場廃止 誤検出をロールバックする(2026-09-02)。

    `register_delisting_events` は Form 25/15 を提出した CIK をシンボルへ解決し、
    その CIK が出した「最も古い提出日」を `tickers.delisted_at` に入れていた。
    AAPL・MA・ABBV のように上場ストラクチャード・ノートの個別シリーズ償還で
    Form 25 を日常的に出す発行体が本体ごと廃止扱いになり、592 件のうち大半が
    「現在も取引中なのに廃止」という誤検出だった。実害:

    - `batch/apply_gates.py:140` が `delisted_at` 付きを included=False にするため、
      2026-09-02 のユニバースで 592 銘柄(全 5893 の約 10%)が丸ごと除外された。
    - `batch/run_daily_collection.py` は `delisted_at IS NULL` で収集対象を絞るので
      この 592 銘柄は日次収集から脱落し、`snapshot_collector` の delisted_at
      自動クリアも `recover-quarantine`(`WHERE ... delisted_at IS NULL`)も
      どちらも収集対象であることが前提のため発火せず、自己修復しなかった。

    判定基準は `last_trade_after_delisting`(タスク②のコレクタ側ガードと同一):
    主張された廃止日 + 猶予 ``DELISTING_TRADING_GRACE_DAYS`` 日より後に約定が
    あれば、その日付では廃止されていないと断定して `delisted_at` をクリアし、
    対応する `delisting_events` 行を削除する。基準を満たさないもの(価格が無い/
    薄い、取引が廃止日以前で途切れている)は本当の廃止かもしれないので触らない。

    ``apply=False`` は件数を数えるだけで一切コミットしない(CLI のドライラン用)。
    戻り値: ``delisted_total`` / ``rolled_back``(誤検出と断定) /
    ``reserved``(判断保留) / ``events_deleted``。
    """
    counts = {"delisted_total": 0, "rolled_back": 0, "reserved": 0, "events_deleted": 0}
    with session_scope() as session:
        query = session.query(Ticker).filter(Ticker.delisted_at.isnot(None))
        if symbols:
            query = query.filter(Ticker.symbol.in_([symbol.upper() for symbol in symbols]))
        tickers = query.all()
        counts["delisted_total"] = len(tickers)
        for ticker in tickers:
            if last_trade_after_delisting(session, ticker.id, ticker.delisted_at.date()) is None:
                counts["reserved"] += 1
                continue
            counts["rolled_back"] += 1
            event_rows = session.query(DelistingEvent).filter(DelistingEvent.ticker_id == ticker.id)
            counts["events_deleted"] += event_rows.count()
            if apply:
                # 誤検出と断定できた銘柄の delisting_events は、すべて同じ誤った
                # delisted_at 由来なので丸ごと消してよい(全 592 件が
                # source='ticker_master_backfill' または 'sec_full_index' の
                # unknown 分類で、実測の決済額・分類は1件も無い)。
                event_rows.delete(synchronize_session=False)
                ticker.delisted_at = None
        if not apply:
            # ドライランでは .count() しか呼んでいないが、将来の追記で書き込みが
            # 混ざっても残さないよう明示的に巻き戻す。
            session.rollback()
    return counts


def backfill_delisting_events_from_tickers(*, observed_at: datetime.datetime | None = None) -> dict[str, int]:
    """Create conservative unknown-classification events from existing master data.

    `Ticker.delisted_at` proves only that listing ceased.  It is intentionally
    not used to infer acquisition, bankruptcy, or settlement economics.
    """
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"eligible": 0, "inserted": 0, "existing": 0}
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.delisted_at.isnot(None)).all()
        counts["eligible"] = len(tickers)
        for ticker in tickers:
            event_date = ticker.delisted_at.date()
            existing = session.query(DelistingEvent.id).filter_by(ticker_id=ticker.id, event_date=event_date, event_type="unknown").first()
            if existing:
                counts["existing"] += 1
                continue
            session.add(DelistingEvent(ticker_id=ticker.id, event_date=event_date, event_type="unknown", source="ticker_master_backfill",
                source_url=None, observed_at=observed_at, confidence="low"))
            counts["inserted"] += 1
    return counts
