"""SEC提出書類メタデータの収集バッチ(30.3.6)。

ユニバースは数千銘柄あり、全件のEDGARを毎日見に行くのは現実的でない(そして
無意味——ほとんどが検討対象にすらならない)。追跡対象は次の和集合とする
(30.3.4):

1. `config/positions.yaml` に載っている保有銘柄(無ければ空)
2. 直近スコア日のランキング上位N件(`max_tracked_tickers` から1と3の分を引いた残り)
3. `research/` にノートが存在する銘柄(検討中の銘柄)

**保有銘柄を必ず含めるのが要点。** ランキング圏外に落ちた保有銘柄こそ 8-K の
監視が要る。順位が下がったこと自体は売る理由にならない(元文書 第12節)が、
監視をやめる理由にもならない。
"""

from __future__ import annotations

import datetime
import logging

from pandas.tseries.holiday import USFederalHolidayCalendar

from sqlalchemy.orm import Session

from autoscreener.collectors.edgar_client import EdgarClient, FilingRecord
from autoscreener.collectors.errors import CollectionError, EmptyResponseError
from autoscreener.config import EdgarConfig, get_settings, load_edgar_config, load_positions_config
from autoscreener.db.models import CollectionCursor, Filing, Score, Ticker
from autoscreener.db.session import session_scope
from autoscreener.research.notes import load_all_notes

logger = logging.getLogger(__name__)

# 30.3.6:収集対象フォーム。この集合に無いフォーム(投資家に無関係な事務文書等)
# は保存しない——`filings` テーブルを無限に太らせないため。
TRACKED_FORMS: frozenset[str] = frozenset(
    {
        "8-K",
        "10-K",
        "10-Q",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "NT 10-K",
        "NT 10-Q",
        "S-3",
        "S-3ASR",
        "424B5",
        "424B4",
        "DEF 14A",
        "4",
        "SC 13D",
        "SC 13G",
        "UPLOAD",
        "CORRESP",
        "25-NSE",
        "15-12B",
    }
)


def select_tracked_tickers(session: Session, *, limit: int) -> list[Ticker]:
    """追跡対象銘柄(30.3.4 の和集合)。CIKが無い銘柄は対象外(何も引けないため)。"""
    positions = load_positions_config()
    position_symbols = {p.ticker.upper() for p in positions.positions if p.closed_on is None}

    note_symbols = set(load_all_notes().keys())

    latest_score_date = session.query(Score.score_date).order_by(Score.score_date.desc()).limit(1).scalar()
    ranked_symbols: list[str] = []
    if latest_score_date is not None:
        remaining = max(0, limit - len(position_symbols | note_symbols))
        rows = (
            session.query(Ticker.symbol)
            .join(Score, Score.ticker_id == Ticker.id)
            .filter(Score.score_date == latest_score_date, Score.probability.isnot(None))
            .order_by(Score.probability.desc())
            .limit(remaining)
            .all()
        )
        ranked_symbols = [r[0] for r in rows]

    all_symbols = position_symbols | note_symbols | set(ranked_symbols)
    if all_symbols:
        tickers = (
            session.query(Ticker)
            .filter(Ticker.symbol.in_(all_symbols), Ticker.cik.isnot(None))
            .limit(limit)
            .all()
        )
        if tickers:
            return tickers

    # A stale watchlist or an unseeded score table must not make every
    # EDGAR-dependent collection a no-op.  Bootstrap with a deterministic
    # active, SEC-mapped slice until the normal tracked universe is available.
    return (
        session.query(Ticker)
        .filter(
            Ticker.cik.isnot(None),
            Ticker.delisted_at.is_(None),
            Ticker.is_benchmark.is_(False),
        )
        .order_by(Ticker.symbol)
        .limit(limit)
        .all()
    )


def _upsert_filings(session: Session, ticker: Ticker, records: list[FilingRecord]) -> int:
    """既存行は上書きしない(`accession_number` は不変)。新規のみ INSERT。"""
    existing = {
        row[0]
        for row in session.query(Filing.accession_number).filter(Filing.ticker_id == ticker.id).all()
    }
    new_count = 0
    for record in records:
        if record.accession_number in existing:
            continue
        session.add(
            Filing(
                ticker_id=ticker.id,
                cik=ticker.cik,
                accession_number=record.accession_number,
                form=record.form,
                filed_date=record.filed_date,
                report_date=record.report_date,
                items=record.items,
                primary_document=record.primary_document,
                document_url=record.document_url,
            )
        )
        new_count += 1
    return new_count


def collect_filings(
    *,
    symbols: list[str] | None = None,
    edgar_config: EdgarConfig | None = None,
    full_refresh: bool = False,
    use_daily_index: bool = False,
    as_of: datetime.date | None = None,
) -> dict[str, int | list[str]]:
    """追跡対象銘柄の提出書類メタデータを取得し `filings` へ upsert する。

    戻り値は {"tickers": n, "new_filings": n, "skipped_no_cik": n, "failures": n}。
    1銘柄の失敗で全体を止めない。
    """
    settings = get_settings()
    config = edgar_config or load_edgar_config()
    if not config.enabled:
        logger.info("edgar collection disabled by config")
        return {"tickers": 0, "new_filings": 0, "skipped_no_cik": 0, "failures": 0}

    client = EdgarClient(config, settings.edgar_user_agent or "")

    counts: dict[str, int | list[str]] = {
        "tickers": 0,
        "new_filings": 0,
        "skipped_no_cik": 0,
        "failures": 0,
        "changed_symbols": [],
        "index_ciks": 0,
    }
    as_of = as_of or datetime.datetime.now(datetime.UTC).date()
    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_tracked_tickers(session, limit=config.max_tracked_tickers)

        cursor = session.query(CollectionCursor).filter_by(
            source="sec_edgar", scope="tracked_filings_daily_index"
        ).one_or_none()
        latest_index_date: datetime.date | None = None
        index_ciks: set[str] = set()
        if use_daily_index and not full_refresh:
            # EDGAR builds each business-day index after 22:00 ET.  At this app's
            # 09:00 JST schedule the immediately preceding U.S. day's index can
            # still be incomplete.  Use one settled-day lag; it is picked up on
            # the next run rather than probing an archive URL that SEC answers
            # with 403 when the file does not exist yet.
            floor = cursor.cursor_date + datetime.timedelta(days=1) if cursor else as_of - datetime.timedelta(days=7)
            end_date = as_of - datetime.timedelta(days=2)
            federal_holidays = {
                timestamp.date()
                for timestamp in USFederalHolidayCalendar().holidays(start=floor, end=end_date)
            }
            # Catch up every unprocessed calendar day after downtime.  Missing
            # weekend/holiday indexes are expected and simply return no data.
            candidate = floor
            while candidate <= end_date:
                if candidate.weekday() >= 5 or candidate in federal_holidays:
                    candidate += datetime.timedelta(days=1)
                    continue
                try:
                    ciks = client.fetch_daily_index_ciks(candidate, forms=set(TRACKED_FORMS))
                except EmptyResponseError:
                    candidate += datetime.timedelta(days=1)
                    continue
                index_ciks.update(ciks)
                latest_index_date = max(latest_index_date or candidate, candidate)
                candidate += datetime.timedelta(days=1)
            priority_symbols = {
                p.ticker.upper() for p in load_positions_config().positions if p.closed_on is None
            } | set(load_all_notes().keys())
            tickers = [
                ticker for ticker in tickers
                if ticker.cik in index_ciks or ticker.symbol in priority_symbols
            ]
            counts["index_ciks"] = len(index_ciks)

        changed_symbols: list[str] = []
        for ticker in tickers:
            if not ticker.cik:
                counts["skipped_no_cik"] = int(counts["skipped_no_cik"]) + 1
                continue
            try:
                records = client.fetch_filings(ticker.cik, forms=TRACKED_FORMS)
            except EmptyResponseError:
                # CIKはあるが提出が無い(新規上場直後等)。失敗として数えない。
                counts["tickers"] = int(counts["tickers"]) + 1
                continue
            except CollectionError:
                logger.exception("%s: failed to fetch filings", ticker.symbol)
                counts["failures"] = int(counts["failures"]) + 1
                continue

            counts["tickers"] = int(counts["tickers"]) + 1
            new_count = _upsert_filings(session, ticker, records)
            counts["new_filings"] = int(counts["new_filings"]) + new_count
            if new_count:
                changed_symbols.append(ticker.symbol)

        counts["changed_symbols"] = sorted(changed_symbols)
        if latest_index_date is not None and int(counts["failures"]) == 0:
            if cursor is None:
                session.add(CollectionCursor(
                    source="sec_edgar",
                    scope="tracked_filings_daily_index",
                    cursor_date=latest_index_date,
                    detail={"ciks": len(index_ciks)},
                ))
            elif latest_index_date > cursor.cursor_date:
                cursor.cursor_date = latest_index_date
                cursor.detail = {"ciks": len(index_ciks)}

    return counts
