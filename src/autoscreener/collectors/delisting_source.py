"""上場廃止ユニバースの構築(docs/defect_and_edge_audit_2026-08-28.md D-1 / I-2)。

**D-1【致命的】**:擬似バックテストの母集団は100%が現在も上場している銘柄で
あり(実測で確認)、v4のパラメータは全部この標本の上でKPIを比較して選ばれて
いる。米国マイクロキャップの年間上場廃止率は概ね4〜7%——3年で母集団の
15〜20%が失われ、その内訳は(a)破綻・上場基準抵触による −80〜−100%、
(b)買収による +20〜+100% の一発、という**両裾**である。

このモジュールは SEC EDGAR の四半期フルインデックス(`form.idx`)を全期間
走査し、上場廃止を示すフォームを提出した企業を CIK 経由でティッカーへ解決し、
`tickers` に `delisted_at = <Form 25/15 の提出日>` 付きで登録する。

- パース(`parse_form_index` / `iter_delisting_events`)は純粋関数。
- `register_delisting_events` が DB へ反映する。**`is_quarantined = True` にしない**
  ——日次収集の対象からは `delisted_at IS NOT NULL` で別途外す。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.db.models import DelistingEvent as DelistingEventRow, PriceSnapshot, Ticker
from autoscreener.db.session import session_scope
from autoscreener.symbols import normalize_symbol

logger = logging.getLogger(__name__)

# Form 25/15 の「提出日」を廃止日として扱うときの猶予日数(2026-09-02 D-1 誤検出対策)。
#
# `register_delisting_events` は Form 25/15 の提出者を CIK→シンボルで解決し、その CIK が
# 出した最古の提出日を `tickers.delisted_at` に入れていた。AAPL・MA・ABBV のように上場
# ストラクチャード・ノートの個別シリーズ償還で Form 25 を日常的に出す発行体が本体ごと
# 廃止扱いになり、592 件中の 500 件超が「現在も取引中なのに廃止」という誤検出になった。
#
# 判定基準は「主張された廃止日より後に取引を続けている銘柄は、その日付では廃止されて
# いない」。ただし SEC Rule 12d2-2(d)(1) では Form 25 提出から上場廃止の発効まで10日
# (Section 12(b) の登録抹消完了まで90日)あり、正当に廃止された銘柄でも提出日
# ——`delisted_at` に入る値——の後 約10日は約定が残る。30日はこの窓の3倍を取り、価格
# フィードの遅延・非取引日・提出日と最終取引日のズレも吸収する。実測の delta(最終取引日
# − 廃止日)分布も 0日以下に5件・7〜17日に7件・その後 33日以降に密集で、18〜32日は空白。
DELISTING_TRADING_GRACE_DAYS = 30


def last_trade_after_delisting(
    session: Session,
    ticker_id: int,
    claimed_delist_date: datetime.date,
    *,
    grace_days: int = DELISTING_TRADING_GRACE_DAYS,
) -> datetime.date | None:
    """`claimed_delist_date` + `grace_days` より後の約定が `price_snapshots` に
    あれば、その最終取引日を返す(= その日付では廃止されていない誤検出の証拠)。

    無ければ `None`。価格が全く無い/薄い銘柄や、取引が廃止日以前で途切れて
    いる銘柄も `None` になる——本当の廃止かもしれないので、呼び出し側はこれらを
    触ってはいけない(推測で廃止判定を外さない)。ロールバック(タスク①)と
    コレクタのガード(タスク②)で同じこの関数を使う。
    """
    cutoff = claimed_delist_date + datetime.timedelta(days=grace_days)
    return (
        session.query(func.max(PriceSnapshot.trade_date))
        .filter(
            PriceSnapshot.ticker_id == ticker_id,
            PriceSnapshot.trade_date > cutoff,
        )
        .scalar()
    )


# 上場廃止を示すフォーム。
#   25 / 25-NSE          … 取引所による上場廃止届
#   15-12B / 15-12G      … 登録抹消(報告義務の終了)
#   15F-12B / 15F-12G    … 外国民間発行体の登録抹消
DELISTING_FORMS: frozenset[str] = frozenset(
    {"25", "25-NSE", "15-12B", "15-12G", "15F-12B", "15F-12G"}
)


@dataclass(frozen=True)
class FormIndexEntry:
    form: str
    company: str
    cik: str  # 10桁ゼロ埋め
    date_filed: datetime.date
    filename: str


@dataclass(frozen=True)
class DelistingEvent:
    cik: str  # 10桁ゼロ埋め
    form: str
    filed_date: datetime.date
    company: str
    filename: str | None = None


def parse_form_index(text: str) -> list[FormIndexEntry]:
    """`form.idx` の生テキストをパースする。

    ヘッダ行(`Form Type  Company Name  CIK  Date Filed  File Name`)と
    区切り線(`-----`)の後に、複数スペース区切りの固定幅レコードが続く。
    列内に複数スペースが入りうる(会社名)ため、右側3列(CIK/日付/ファイル名)を
    末尾から確定し、残りを form / company に割り当てる。
    """
    entries: list[FormIndexEntry] = []
    started = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not started:
            if set(line) <= {"-", " "} and "-" in line:
                started = True
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        filename = parts[-1]
        date_str = parts[-2]
        cik_str = parts[-3]
        try:
            date_filed = datetime.date.fromisoformat(date_str)
            cik = f"{int(cik_str):010d}"
        except ValueError:
            continue
        form = parts[0]
        company = " ".join(parts[1:-3])
        entries.append(
            FormIndexEntry(
                form=form, company=company, cik=cik, date_filed=date_filed, filename=filename
            )
        )
    return entries


def iter_delisting_events(entries: Iterable[FormIndexEntry]) -> Iterator[DelistingEvent]:
    """`form.idx` エントリから上場廃止フォームだけを取り出す。"""
    for entry in entries:
        if entry.form in DELISTING_FORMS:
            yield DelistingEvent(
                cik=entry.cik,
                form=entry.form,
                filed_date=entry.date_filed,
                company=entry.company,
                filename=entry.filename,
            )


def _quarter_range(
    start: datetime.date, end: datetime.date
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, quarter = start.year, (start.month - 1) // 3 + 1
    end_key = (end.year, (end.month - 1) // 3 + 1)
    while (year, quarter) <= end_key:
        out.append((year, quarter))
        quarter += 1
        if quarter > 4:
            quarter, year = 1, year + 1
    return out


def collect_delisting_events(
    start: datetime.date,
    end: datetime.date | None = None,
    client: EdgarClient | None = None,
) -> list[DelistingEvent]:
    """`start`〜`end` の全四半期の `form.idx` を取得し、上場廃止イベントを返す。

    価格履歴の開始(現状 2023-08)より1年前から今日まで走らせるのが D-1 の推奨。
    """
    end = end or datetime.date.today()
    if client is None:
        client = EdgarClient(load_edgar_config(), get_settings().edgar_user_agent or "")
    events: list[DelistingEvent] = []
    for year, quarter in _quarter_range(start, end):
        try:
            text = client.fetch_full_index_form(year, quarter)
        except Exception:  # noqa: BLE001 — 1四半期の失敗で全体を止めない
            logger.exception("failed to fetch form.idx for %sQ%s", year, quarter)
            continue
        entries = parse_form_index(text)
        quarter_events = list(iter_delisting_events(entries))
        logger.info("%sQ%s: %d delisting filings", year, quarter, len(quarter_events))
        events.extend(quarter_events)
    return events


def register_delisting_events(
    events: list[DelistingEvent],
    cik_to_symbol: dict[str, str] | None = None,
    client: EdgarClient | None = None,
) -> dict[str, int]:
    """解決できた上場廃止イベントを `tickers.delisted_at` に登録する。

    - `cik_to_symbol` 未指定なら `company_tickers.json` から作る(**現在の**上場
      企業しか載っていないので解決率は限定的。`tickers.cik` に既に入っている
      廃止済み企業も併用する)。
    - 同一シンボルに複数イベントがあれば**最も古い提出日**を採用する。
    - `is_quarantined` は変更しない(日次収集の除外は `delisted_at IS NOT NULL`)。
    - **ガード(2026-09-02 D-1 誤検出対策)**:Form 25/15 の提出日より後に価格が
      観測されている銘柄は登録しない。上場ノートのシリーズ償還で Form 25 を出す
      発行体(AAPL・MA 等)を本体ごと廃止扱いにするのを防ぐだけで、原因や決済額を
      推測しに行くわけではない(わからないものは unknown のまま)。スキップ件数は
      `counts["skipped_recent_trading"]` で呼び出し側に見える。
    """
    if cik_to_symbol is None:
        if client is None:
            client = EdgarClient(load_edgar_config(), get_settings().edgar_user_agent or "")
        cik_to_symbol = client.fetch_company_tickers()  # {symbol: cik}
        cik_to_symbol = {cik: sym for sym, cik in cik_to_symbol.items()}

    earliest: dict[str, DelistingEvent] = {}
    with session_scope() as session:
        # 既知の cik → symbol(廃止済み企業も tickers に cik が入っていることがある)
        for symbol, cik in session.query(Ticker.symbol, Ticker.cik).filter(Ticker.cik.isnot(None)).all():
            cik_to_symbol.setdefault(cik, symbol)

        for event in events:
            symbol = cik_to_symbol.get(event.cik)
            if not symbol:
                continue
            symbol = normalize_symbol(symbol)
            current = earliest.get(symbol)
            if current is None or event.filed_date < current.filed_date:
                earliest[symbol] = event

        counts = {
            "resolved": 0,
            "registered": 0,
            "updated": 0,
            "unresolved": len(events),
            "event_rows": 0,
            "skipped_recent_trading": 0,
        }
        for symbol, event in earliest.items():
            counts["resolved"] += 1
            filed_dt = datetime.datetime.combine(
                event.filed_date, datetime.time(), tzinfo=datetime.UTC
            )
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            # ガード:提出日 + 猶予日数より後に約定がある銘柄は、その日付では廃止
            # されていない。CIK→シンボル解決が拾う「ノートのシリーズ償還を出す
            # 本体」を弾く。ticker が未登録なら価格も無いので判定は走らない。
            if ticker is not None and (
                last_trade_after_delisting(session, ticker.id, event.filed_date) is not None
            ):
                counts["skipped_recent_trading"] += 1
                continue
            if ticker is None:
                ticker = Ticker(
                        symbol=symbol,
                        market="US",
                        cik=event.cik,
                        delisted_at=filed_dt,
                    )
                session.add(ticker)
                session.flush()
                counts["registered"] += 1
            elif ticker.delisted_at is None:
                ticker.delisted_at = filed_dt
                ticker.cik = ticker.cik or event.cik
                counts["updated"] += 1
            existing = session.query(DelistingEventRow.id).filter_by(
                ticker_id=ticker.id, event_date=event.filed_date, event_type="unknown"
            ).first()
            if not existing:
                session.add(DelistingEventRow(
                    ticker_id=ticker.id,
                    event_date=event.filed_date,
                    event_type="unknown",
                    source="sec_full_index",
                    source_url=(f"https://www.sec.gov/Archives/{event.filename}" if event.filename else None),
                    observed_at=datetime.datetime.now(datetime.UTC),
                    confidence="low",
                ))
                counts["event_rows"] += 1
    return counts
