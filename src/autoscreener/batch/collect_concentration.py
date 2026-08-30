"""顧客集中度(10%超顧客の開示)の収集バッチ(K-3)。

`research/TEMPLATE.md` が要求する先行指標 `customer_concentration_disclosed_drop`
(`screening/monitoring_metrics.py` の `evaluate_customer_concentration_metric`)
の実測値を作る。K-2が保存した `filing_sections` の `item1`(事業)/`item7`
(MD&A)本文を `screening.customer_concentration.parse_concentration_text` で
読み、加えて XBRL `ConcentrationRiskPercentage1` を `extract_from_xbrl` で読み、
両方を `customer_concentration` に upsert する。

**本文抽出を主経路として扱う。** `extract_from_xbrl` は軸(segment)情報の無い
companyconcept ペイロードに対しては常に空を返す仕様(誤検出防止。詳細は
`screening/customer_concentration.py` のdocstring)なので、現行のSEC APIでは
実質的にtextソースの行しか生まれない。それでも将来のAPI拡張に備えてxbrl経路
は残す。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.collectors.errors import CollectionError, EmptyResponseError
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.dates import utc_today
from autoscreener.db.models import CustomerConcentration, Filing, FilingSection, Ticker
from autoscreener.db.session import session_scope
from autoscreener.screening.customer_concentration import (
    extract_from_xbrl,
    parse_concentration_text,
)

logger = logging.getLogger(__name__)

_DEFAULT_TICKER_LIMIT = 300

# XBRLコンセプトを1つ引く関数。`cik` を受け取り companyconcept の生JSONを返す。
XbrlFetcher = Callable[[str], dict]


def _default_xbrl_fetcher() -> XbrlFetcher:
    """既定の取得経路(実EDGAR)。`collect_filing_sections._default_source` と
    同じ理由で、呼び出し時まで `EdgarClient` を作らない。"""
    settings = get_settings()
    config = load_edgar_config()
    client = EdgarClient(config, settings.edgar_user_agent or "")
    return lambda cik: client.fetch_company_concept(cik, "us-gaap", "ConcentrationRiskPercentage1")


def _existing_labels(session: Session, ticker_id: int, period_end: datetime.date) -> set[str]:
    rows = (
        session.query(CustomerConcentration.customer_label)
        .filter_by(ticker_id=ticker_id, period_end=period_end)
        .all()
    )
    return {r[0] for r in rows}


def _upsert_one(
    session: Session,
    ticker: Ticker,
    period_end: datetime.date,
    fiscal_year: int | None,
    label: str,
    revenue_pct: float,
    source: str,
    accession: str | None,
    existing_labels: set[str],
    today: datetime.date,
    counts: dict[str, int],
) -> None:
    """`(ticker_id, period_end, customer_label)` が既存なら何もしない
    (`uq_customer_concentration`。DB制約と同じキーでアプリ側でも重複を避ける
    ——INSERT時のIntegrityErrorでトランザクション全体を巻き込みたくない)。"""
    if label in existing_labels:
        counts["existing"] += 1
        return
    session.add(
        CustomerConcentration(
            ticker_id=ticker.id,
            period_end=period_end,
            fiscal_year=fiscal_year,
            customer_label=label,
            revenue_pct=revenue_pct,
            source=source,
            source_accession=accession,
            collected_on=today,
        )
    )
    existing_labels.add(label)
    counts["new_rows"] += 1


def _collect_from_text(
    session: Session, ticker: Ticker, today: datetime.date, counts: dict[str, int]
) -> None:
    sections = (
        session.query(FilingSection)
        .filter(
            FilingSection.ticker_id == ticker.id,
            FilingSection.section.in_(["item1", "item7"]),
        )
        .order_by(FilingSection.filed_date.desc())
        .all()
    )
    if not sections:
        counts["no_filing_sections"] += 1
        return

    for section in sections:
        mentions = parse_concentration_text(section.text)
        if not mentions:
            continue
        filing = session.query(Filing).filter_by(accession_number=section.accession_number).first()
        # 14.3と同じくポイントインタイムの基準は提出日側だが、`customer_
        # concentration.period_end` は「どの会計年度の開示か」を表すため、
        # フィリングの決算期末(report_date)を優先し、無ければ提出日で代用する。
        period_end = filing.report_date if filing is not None and filing.report_date else section.filed_date
        fiscal_year = period_end.year if period_end else None
        existing_labels = _existing_labels(session, ticker.id, period_end)
        for mention in mentions:
            _upsert_one(
                session,
                ticker,
                period_end,
                fiscal_year,
                mention.customer_label,
                mention.revenue_pct,
                "text",
                section.accession_number,
                existing_labels,
                today,
                counts,
            )


def _collect_from_xbrl(
    session: Session, ticker: Ticker, fetcher: XbrlFetcher, today: datetime.date, counts: dict[str, int]
) -> None:
    if not ticker.cik:
        return
    payload = fetcher(ticker.cik)
    if not payload:
        return
    for fact in extract_from_xbrl(payload):
        existing_labels = _existing_labels(session, ticker.id, fact.period_end)
        _upsert_one(
            session,
            ticker,
            fact.period_end,
            fact.fiscal_year,
            fact.customer_label,
            fact.revenue_pct,
            "xbrl",
            fact.accession,
            existing_labels,
            today,
            counts,
        )


def collect_concentration(
    symbols: list[str] | None = None,
    *,
    limit: int = _DEFAULT_TICKER_LIMIT,
    xbrl_fetcher: XbrlFetcher | None = None,
) -> dict[str, int]:
    """`filing_sections`(本文)とXBRLの両方から顧客集中を抽出し、
    `customer_concentration` に upsert する。

    戻り値: {"tickers", "new_rows", "existing", "no_filing_sections", "failures"}。
    `CollectionError` 系の例外は銘柄単位で握って次へ進む(全体を止めない)。
    """
    today = utc_today()
    fetcher = xbrl_fetcher or _default_xbrl_fetcher()
    counts = {
        "tickers": 0,
        "new_rows": 0,
        "existing": 0,
        "no_filing_sections": 0,
        "xbrl_not_reported": 0,
        "failures": 0,
    }

    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_tracked_tickers(session, limit=limit)

        for ticker in tickers:
            counts["tickers"] += 1
            try:
                _collect_from_text(session, ticker, today, counts)
            except CollectionError:
                logger.exception("%s: concentration text collection failed", ticker.symbol)
                counts["failures"] += 1
                continue

            try:
                _collect_from_xbrl(session, ticker, fetcher, today, counts)
            except EmptyResponseError:
                # This optional company-concept tag is absent for most issuers.
                # Its 404 must not discard disclosures recovered from filing text.
                counts["xbrl_not_reported"] += 1
            except CollectionError:
                logger.exception("%s: concentration XBRL collection failed", ticker.symbol)
                counts["failures"] += 1
                continue

    return counts
