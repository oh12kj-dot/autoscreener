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

from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.db.models import DelistingEvent as DelistingEventRow, Ticker
from autoscreener.db.session import session_scope
from autoscreener.symbols import normalize_symbol

logger = logging.getLogger(__name__)

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

        counts = {"resolved": 0, "registered": 0, "updated": 0, "unresolved": len(events), "event_rows": 0}
        for symbol, event in earliest.items():
            counts["resolved"] += 1
            filed_dt = datetime.datetime.combine(
                event.filed_date, datetime.time(), tzinfo=datetime.UTC
            )
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
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
