"""SEC提出書類メタデータの収集バッチ(30.3.6)。

ユニバースは数千銘柄あり、全件のEDGARを毎日見に行くのは現実的でない(そして
無意味——ほとんどが検討対象にすらならない)。追跡対象は次の和集合とする
(30.3.4):

1. `config/positions.yaml` に載っている保有銘柄(無ければ空)
2. 直近スコア日のランキング上位N件(`max_tracked_tickers` から1・3・4の分を引いた残り)
3. `research/` にノートが存在する銘柄(検討中の銘柄)
4. `delisted_at` 確定後 `POST_DELISTING_FILING_WINDOW_DAYS` 以内の銘柄
   (2026-09-05追加。CIK共有で現役銘柄と紐付く候補は除外——理由は
   `_recently_delisted_trackable_symbols` のdocstring)

**保有銘柄を必ず含めるのが要点。** ランキング圏外に落ちた保有銘柄こそ 8-K の
監視が要る。順位が下がったこと自体は売る理由にならない(元文書 第12節)が、
監視をやめる理由にもならない。

**上場廃止直後こそ収集を止めてはいけない。** Form 25/15そのもの、8-K
Item 1.03(破産)/2.01(資産処分完了)/3.01(上場基準抵触通知)、DEFM14A、
Schedule 13E-3は`delisted_at`確定の前後に集中して提出されるが、従来は
保有クローズ・ランキング脱落・ノート削除のいずれかで銘柄が1〜3のどれにも
残らなくなった時点で収集が事実上止まっていた
(docs/delisting_label_backfill_2026-09-04.md §3、94件中93件が`filings`に
1行も持たない直接の原因)。4.はこの穴を塞ぐ。
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
#
# 2026-09-05(docs/delisting_label_backfill_2026-09-04.md §3、
# docs/post_delisting_evidence_collection_2026-09-05.md):上場廃止の原因・決済額を
# 語る決定的なフォームが漏れていた。追加分は
# `collectors/delisting_classification.py` の `_DEREGISTRATION_FORMS` /
# `_GOING_PRIVATE_FORMS` / `_MERGER_PROXY_FORMS` が実際に探しに行くフォームと
# 一致させてある(そちらが変われば両方更新すること)——
#   25 / 15F-12B / 15F-12G … 取引所発の上場廃止届・外国民間発行体の登録抹消
#     (25-NSE・15-12Bは元から追跡対象。25そのものと15-12G・外国発行体版が抜けていた)
#   SC 13E3 / SC 13E-3      … 非公開化(going-private)届出書。EDGARの正規化表記は
#                             ハイフン無し"SC 13E3"だが、揺れに備え両方入れる
#   DEFM14A                 … 合併の委任状勧誘書類(対価が現金/株式か・金額を含む)
# 8-K自体は元から追跡対象であり、Item 1.03(破産)/2.01(資産処分完了)/
# 3.01(上場基準抵触通知)はどれも8-Kの`items`配列に載る——`edgar_client.py`の
# `fetch_filings`はフォーム単位でしか絞り込まず、8-K内のitem番号では絞らないため
# 追加のフォーム登録は不要(既にどのitemも保存されている)。
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
        "25",
        "25-NSE",
        "15-12B",
        "15-12G",
        "15F-12B",
        "15F-12G",
        "SC 13E3",
        "SC 13E-3",
        "DEFM14A",
    }
)

# 2026-09-05(docs/post_delisting_evidence_collection_2026-09-05.md):上場廃止の
# 原因を語るフォーム(Form 25/15そのもの、8-K Item 1.03/2.01/3.01、DEFM14A、
# SC 13E3)はまさに`delisted_at`が確定する前後に集中して提出されるにもかかわらず、
# 収集は`delisted_at`が立った瞬間に止まっていた(94件中93件が`filings`に1行も
# 持たない直接の原因、docs/delisting_label_backfill_2026-09-04.md §3)。
#
# 窓の根拠(推測ではなくSECの制度上の期限から逆算):
#   - SEC Rule 12d2-2(d)(1):取引所によるForm 25提出から上場廃止の効力発生まで10日
#   - Exchange Act Section 12(b)/12(g):登録抹消(Form 15の効力発生)まで最大90日
#   - `collectors/delisting_source.py`の`DELISTING_TRADING_GRACE_DAYS=30`は
#     「価格が廃止後も動き続けているか」を見る別の窓で、実測の最終取引日ズレ分布
#     (0日以下5件・7〜17日7件・33日以降に密集)を根拠にしている。フォームの
#     提出は最終取引よりさらに後(合併委任状の確定・破産手続きの追加8-K等)まで
#     続きうるため、価格用の30日をそのまま流用しない。
# 90日を採用する——Form 15の効力発生上限(Section 12(b)/12(g))と同じ長さに
# 揃えることで、「登録抹消の手続きが完全に終わるまでは証拠を拾いに行く」という
# 一貫した基準になる。決算日をまたぐ合併委任状(DEFM14A)がこの窓の外に出る
# ケースはなお残りうるが、根拠のない値(例:180日・365日)を足で伸ばすより、
# 制度上の期限に揃えた値のほうが正当化できる。
POST_DELISTING_FILING_WINDOW_DAYS = 90


def _recently_delisted_trackable_symbols(
    session: Session, *, window_days: int = POST_DELISTING_FILING_WINDOW_DAYS
) -> set[str]:
    """`delisted_at`確定後`window_days`以内のCIK保有銘柄——原因を語るフォームが
    出続けている猶予期間(モジュール定数のコメント参照)。

    **CIK共有の罠(docs/delisting_label_backfill_2026-09-04.md §2、実例:
    現役TDWと廃止済みTDGMWが同一CIK)を避けるため、現役銘柄(`delisted_at IS
    NULL`)と同じCIKを持つ候補は除外する。** `EdgarClient.fetch_filings`は
    CIK単位でしか引けないため、この除外をしないと現役銘柄の通常運用中の
    提出書類が丸ごと廃止銘柄の`ticker_id`へ収集されてしまう——
    `delisting_classification.py`側の`ambiguous_shared_cik`はDB反映を止める
    だけで、収集自体がこの汚染を作ってしまえば手遅れになる。
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=window_days)
    candidates = (
        session.query(Ticker.symbol, Ticker.cik)
        .filter(
            Ticker.delisted_at.isnot(None),
            Ticker.delisted_at >= cutoff,
            Ticker.cik.isnot(None),
        )
        .order_by(Ticker.symbol)
        .all()
    )
    if not candidates:
        return set()
    active_ciks = {
        row[0]
        for row in session.query(Ticker.cik)
        .filter(Ticker.delisted_at.is_(None), Ticker.cik.isnot(None))
        .order_by(Ticker.cik)
        .all()
    }
    return {symbol for symbol, cik in candidates if cik not in active_ciks}


def select_tracked_tickers(session: Session, *, limit: int) -> list[Ticker]:
    """追跡対象銘柄(30.3.4 の和集合)。CIKが無い銘柄は対象外(何も引けないため)。

    2026-09-05:直近`delisted_at`が確定した銘柄も、原因を語るフォームが出続ける
    猶予期間内は明示的に追加する(`_recently_delisted_trackable_symbols`)——
    さもないと保有クローズ・ランキング圏外・ノート削除のいずれかで自然に
    追跡対象から落ち、`delisted_at`確定と同時に収集が事実上止まる
    (docs/delisting_label_backfill_2026-09-04.md §3)。
    """
    positions = load_positions_config()
    position_symbols = {p.ticker.upper() for p in positions.positions if p.closed_on is None}

    note_symbols = set(load_all_notes().keys())
    recently_delisted_symbols = _recently_delisted_trackable_symbols(session)

    latest_score_date = session.query(Score.score_date).order_by(Score.score_date.desc()).limit(1).scalar()
    ranked_symbols: list[str] = []
    if latest_score_date is not None:
        remaining = max(
            0, limit - len(position_symbols | note_symbols | recently_delisted_symbols)
        )
        rows = (
            session.query(Ticker.symbol)
            .join(Score, Score.ticker_id == Ticker.id)
            .filter(Score.score_date == latest_score_date, Score.probability.isnot(None))
            .order_by(Score.probability.desc())
            .limit(remaining)
            .all()
        )
        ranked_symbols = [r[0] for r in rows]

    all_symbols = position_symbols | note_symbols | recently_delisted_symbols | set(ranked_symbols)
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
            # 保有銘柄・ノート銘柄と同様、直近廃止銘柄も「その日の日次インデックスに
            # 出現したCIKだけ」に絞らず毎回`fetch_filings`を通す(2026-09-05)。
            # 猶予期間は最長90日・対象は通常ごく少数(実測94件/約4年)であり、
            # 追跡ティッカー数自体の増分にすぎない——1銘柄あたりのリクエスト数は
            # 従来どおり1回のまま(edgar.requests_per_secondは変えない)。日次
            # インデックス側の欠落日・スキャン窓の外縁で決定的なフォームを取り
            # こぼすと元の木阿弥なので、この狭い窓では確実性を優先する。
            priority_symbols = {
                p.ticker.upper() for p in load_positions_config().positions if p.closed_on is None
            } | set(load_all_notes().keys()) | _recently_delisted_trackable_symbols(session)
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
