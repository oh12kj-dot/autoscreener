"""Append-only TAM / market-opportunity writer.

Only source-backed estimates are persisted.  This module deliberately does not
ask an LLM to invent a market size and is never called by an HTTP detail view.
"""

from __future__ import annotations

import datetime
import re

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.coverage import CoverageReasonCode, CoverageStatus
from autoscreener.db.models import FilingSection, LiveDatasetCoverage, MarketOpportunityComponent, MarketOpportunityEstimate, Ticker
from autoscreener.db.session import session_scope
from autoscreener.research.notes import load_all_notes

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_TAM_RE = re.compile(rf"\b(?:total|serviceable)\s+(?:addressable|available)\s+market\b[^$]{{0,120}}\$({_NUMBER})\s*(thousand|million|billion)?", re.IGNORECASE)


def _as_date(value, fallback: datetime.date) -> datetime.date:
    if isinstance(value, datetime.datetime): return value.date()
    if isinstance(value, datetime.date): return value
    if isinstance(value, str): return datetime.date.fromisoformat(value)
    return fallback


def _amount(number: str, scale: str | None) -> float:
    return float(number.replace(",", "")) * _SCALE.get((scale or "").lower(), 1)


def collect_market_opportunity(*, symbols: list[str] | None = None,
                               observed_at: datetime.datetime | None = None) -> dict[str, int]:
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"targets": 0, "manual": 0, "company_reported": 0, "components": 0, "with_data": 0, "no_finding": 0, "failed": 0}
    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([item.upper() for item in symbols])).all()
        else:
            from autoscreener.config import load_edgar_config
            tickers = select_tracked_tickers(session, limit=load_edgar_config().max_tracked_tickers)
        notes = load_all_notes()
        counts["targets"] = len(tickers)
        for ticker in tickers:
            wrote = False
            try:
                note = notes.get(ticker.symbol)
                for item in (note.front_matter.get("market_opportunity") or []) if note else []:
                    if not isinstance(item, dict):
                        continue
                    method = str(item.get("method") or "manual")
                    source_url = item.get("source_url")
                    excerpt = item.get("source_excerpt")
                    has_value = item.get("tam_value") is not None or item.get("components")
                    if not source_url or not excerpt or not has_value or method not in {"manual", "bottom_up", "third_party", "company_reported"}:
                        continue
                    as_of = _as_date(item.get("as_of"), observed_at.date())
                    estimate = _insert_estimate(session, ticker.id, observed_at, as_of, method, item, "research_note")
                    if estimate is not None:
                        counts["manual"] += 1
                        wrote = True
                    counts["components"] += _insert_components(session, estimate, item.get("components") or []) if estimate else 0
                sections = session.query(FilingSection).filter(FilingSection.ticker_id == ticker.id).all()
                for section in sections:
                    if not section.source_url:
                        continue
                    for match in _TAM_RE.finditer(section.text):
                        excerpt = match.group(0)[:500]
                        item = {"tam_value": _amount(match.group(1), match.group(2)), "currency": "USD", "formula_text": "Company-disclosed TAM", "source_url": section.source_url, "source_excerpt": excerpt, "confidence": "low", "created_by": "machine"}
                        estimate = _insert_estimate(session, ticker.id, observed_at, section.filed_date, "company_reported", item, "sec_edgar")
                        if estimate is not None:
                            counts["company_reported"] += 1
                            wrote = True
                if wrote:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_WITH_DATA, None, None)
                    counts["with_data"] += 1
                elif sections:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_NO_FINDING, CoverageReasonCode.SOURCE_SCANNED_NO_MATCH, "No source-backed TAM disclosure")
                    counts["no_finding"] += 1
                elif note is None:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.NOT_COLLECTED, CoverageReasonCode.USER_INPUT_MISSING, "No research note and no SEC section")
                    counts["no_finding"] += 1
                else:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.NOT_COLLECTED, CoverageReasonCode.NO_SUPPORTED_FILING, "No saved filing section")
                    counts["no_finding"] += 1
            except Exception as exc:
                _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTION_FAILED, CoverageReasonCode.PARSE_ERROR, type(exc).__name__, retryable=False)
                counts["failed"] += 1
    return counts


def _insert_estimate(session, ticker_id: int, observed_at: datetime.datetime, as_of: datetime.date, method: str, item: dict, source: str):
    excerpt = str(item["source_excerpt"])
    existing = session.query(MarketOpportunityEstimate).filter_by(ticker_id=ticker_id, as_of=as_of, method=method, source=source, source_excerpt=excerpt).first()
    if existing is not None:
        return existing
    tam = float(item["tam_value"]) if item.get("tam_value") is not None else None
    revenue = float(item["current_revenue_addressable"]) if item.get("current_revenue_addressable") is not None else None
    same_currency = bool(item.get("currency")) and item.get("revenue_currency", item.get("currency")) == item.get("currency")
    penetration = revenue / tam if tam and tam > 0 and revenue is not None and same_currency else None
    estimate = MarketOpportunityEstimate(ticker_id=ticker_id, observed_at=observed_at, as_of=as_of, method=method,
        tam_value=tam, sam_value=float(item["sam_value"]) if item.get("sam_value") is not None else None,
        current_revenue_addressable=revenue, penetration_rate=penetration, currency=item.get("currency"), formula_text=item.get("formula_text"),
        source=source, source_url=str(item["source_url"]), source_excerpt=excerpt, confidence=str(item.get("confidence") or "low"),
        created_by=str(item.get("created_by") or "manual"), raw_payload={"input": item}, coverage_status=CoverageStatus.COLLECTED_WITH_DATA)
    session.add(estimate); session.flush()
    return estimate


def _insert_components(session, estimate: MarketOpportunityEstimate, components: list) -> int:
    inserted = 0
    for item in components:
        if not isinstance(item, dict) or not item.get("component_name"):
            continue
        existing = session.query(MarketOpportunityComponent.id).filter_by(estimate_id=estimate.id, component_name=str(item["component_name"])).first()
        if existing is not None: continue
        session.add(MarketOpportunityComponent(estimate_id=estimate.id, component_name=str(item["component_name"]), quantity=item.get("quantity"),
            unit=item.get("unit"), price_per_unit=item.get("price_per_unit"), penetration_assumption=item.get("penetration_assumption"), result_value=item.get("result_value")))
        inserted += 1
    return inserted


def _ledger(session, ticker_id: int, observed_at: datetime.datetime, status: CoverageStatus, reason: CoverageReasonCode | None,
            detail: str | None, retryable: bool | None = None) -> None:
    session.add(LiveDatasetCoverage(ticker_id=ticker_id, dataset="market_opportunity", observed_at=observed_at, attempted_at=observed_at,
        source="research_note+sec_edgar", source_scope="research/<TICKER>.md; SEC filing sections", coverage_status=status,
        reason_code=reason, reason_detail=detail, retryable=retryable, confidence="medium"))
