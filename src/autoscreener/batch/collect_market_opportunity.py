"""Append-only TAM / market-opportunity writer.

Only source-backed estimates are persisted.  This module deliberately does not
ask an LLM to invent a market size and is never called by an HTTP detail view.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.coverage import CoverageReasonCode, CoverageStatus
from autoscreener.db.models import FilingSection, LiveDatasetCoverage, MarketOpportunityComponent, MarketOpportunityEstimate, Ticker
from autoscreener.db.session import session_scope
from autoscreener.research.notes import load_all_notes

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_TAM_RE = re.compile(
    rf"\b(?:total|serviceable)\s+(?:addressable|available)\s+market\b"
    rf"[^$]{{0,120}}\$({_NUMBER})\s*(thousand|million|billion|trillion)?",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_PLAUSIBLE_TAM = 1e15
_MAX_TAM_TO_REVENUE = 1e6
_MIN_MACHINE_EXTRACTED_TAM = 1e6


@dataclass(frozen=True)
class MarketOpportunityValidation:
    valid: bool
    reason: str | None
    penetration_rate: float | None
    confidence: str


def _as_date(value, fallback: datetime.date) -> datetime.date:
    if isinstance(value, datetime.datetime): return value.date()
    if isinstance(value, datetime.date): return value
    if isinstance(value, str): return datetime.date.fromisoformat(value)
    return fallback


def _amount(number: str, scale: str | None) -> float:
    if scale is None:
        raise ValueError("SEC TAM match has no explicit scale")
    return float(number.replace(",", "")) * _SCALE[scale.lower()]


def validate_market_opportunity(item: dict, *, machine_extracted: bool = False) -> MarketOpportunityValidation:
    """Validate a TAM observation before it can become model input.

    Missing values remain missing; they are never converted to zero. Machine
    extraction is held to a stricter absolute sanity floor because a missing
    textual scale previously persisted ``$1.0`` as one dollar.
    """
    try:
        tam = float(item["tam_value"])
    except (KeyError, TypeError, ValueError):
        return MarketOpportunityValidation(False, "tam_value_missing_or_invalid", None, "low")
    if tam <= 0:
        return MarketOpportunityValidation(False, "tam_value_nonpositive", None, "low")
    if tam > _MAX_PLAUSIBLE_TAM:
        return MarketOpportunityValidation(False, "tam_value_absurd_magnitude", None, "low")
    if machine_extracted and tam < _MIN_MACHINE_EXTRACTED_TAM:
        return MarketOpportunityValidation(False, "machine_tam_absurdly_small", None, "low")

    currency = str(item.get("currency") or "").upper()
    if not _CURRENCY_RE.fullmatch(currency):
        return MarketOpportunityValidation(False, "currency_missing_or_invalid", None, "low")

    revenue_raw = item.get("current_revenue_addressable")
    if revenue_raw is None:
        penetration = None
    else:
        try:
            revenue = float(revenue_raw)
        except (TypeError, ValueError):
            return MarketOpportunityValidation(False, "current_revenue_invalid", None, "low")
        if revenue < 0:
            return MarketOpportunityValidation(False, "current_revenue_negative", None, "low")
        revenue_currency = str(item.get("revenue_currency") or "").upper()
        if not _CURRENCY_RE.fullmatch(revenue_currency) or revenue_currency != currency:
            penetration = None
        elif tam < revenue:
            return MarketOpportunityValidation(False, "tam_below_addressable_revenue", None, "low")
        elif revenue > 0 and tam / revenue > _MAX_TAM_TO_REVENUE:
            return MarketOpportunityValidation(False, "tam_to_revenue_absurd_magnitude", None, "low")
        else:
            penetration = revenue / tam

    requested_confidence = str(item.get("confidence") or "low").lower()
    confidence = "low" if machine_extracted else requested_confidence
    return MarketOpportunityValidation(True, None, penetration, confidence)


def collect_market_opportunity(*, symbols: list[str] | None = None,
                               observed_at: datetime.datetime | None = None) -> dict[str, int]:
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"targets": 0, "manual": 0, "company_reported": 0, "components": 0,
              "with_data": 0, "no_finding": 0, "failed": 0, "rejected_invalid": 0}
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
            validation_errors: list[str] = []
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
                    validation = validate_market_opportunity(item, machine_extracted=False)
                    if not validation.valid:
                        counts["rejected_invalid"] += 1
                        validation_errors.append(validation.reason or "invalid_market_opportunity")
                        continue
                    estimate = _insert_estimate(
                        session, ticker.id, observed_at, as_of, method, item, "research_note", validation
                    )
                    if estimate is not None:
                        counts["manual"] += 1
                        wrote = True
                    counts["components"] += _insert_components(session, estimate, item.get("components") or []) if estimate else 0
                sections = session.query(FilingSection).filter(FilingSection.ticker_id == ticker.id).all()
                for section in sections:
                    if not section.source_url:
                        continue
                    for match in _TAM_RE.finditer(section.text):
                        if match.group(2) is None:
                            counts["rejected_invalid"] += 1
                            validation_errors.append("sec_tam_scale_missing")
                            continue
                        excerpt = match.group(0)[:500]
                        item = {"tam_value": _amount(match.group(1), match.group(2)), "currency": "USD", "formula_text": "Company-disclosed TAM", "source_url": section.source_url, "source_excerpt": excerpt, "confidence": "low", "created_by": "machine"}
                        validation = validate_market_opportunity(item, machine_extracted=True)
                        if not validation.valid:
                            counts["rejected_invalid"] += 1
                            validation_errors.append(validation.reason or "invalid_market_opportunity")
                            continue
                        estimate = _insert_estimate(
                            session, ticker.id, observed_at, section.filed_date,
                            "company_reported", item, "sec_edgar", validation,
                        )
                        if estimate is not None:
                            counts["company_reported"] += 1
                            wrote = True
                if wrote:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_WITH_DATA, None, None)
                    counts["with_data"] += 1
                elif validation_errors:
                    _ledger(
                        session, ticker.id, observed_at, CoverageStatus.COLLECTION_FAILED,
                        CoverageReasonCode.PARSE_ERROR, ",".join(sorted(set(validation_errors))), retryable=False,
                    )
                    counts["failed"] += 1
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


def _insert_estimate(session, ticker_id: int, observed_at: datetime.datetime, as_of: datetime.date,
                     method: str, item: dict, source: str, validation: MarketOpportunityValidation):
    excerpt = str(item["source_excerpt"])
    existing = session.query(MarketOpportunityEstimate).filter_by(ticker_id=ticker_id, as_of=as_of, method=method, source=source, source_excerpt=excerpt).first()
    if existing is not None:
        return existing
    tam = float(item["tam_value"]) if item.get("tam_value") is not None else None
    revenue = float(item["current_revenue_addressable"]) if item.get("current_revenue_addressable") is not None else None
    estimate = MarketOpportunityEstimate(ticker_id=ticker_id, observed_at=observed_at, as_of=as_of, method=method,
        tam_value=tam, sam_value=float(item["sam_value"]) if item.get("sam_value") is not None else None,
        current_revenue_addressable=revenue, penetration_rate=validation.penetration_rate,
        currency=str(item.get("currency")).upper(), formula_text=item.get("formula_text"),
        source=source, source_url=str(item["source_url"]), source_excerpt=excerpt,
        confidence=validation.confidence, created_by=str(item.get("created_by") or "manual"),
        raw_payload={"input": item, "validation_version": "v5_phase0"},
        coverage_status=CoverageStatus.COLLECTED_WITH_DATA)
    session.add(estimate); session.flush()
    return estimate


def revalidate_market_opportunity_estimates(*, apply: bool = False,
                                            observed_at: datetime.datetime | None = None) -> dict[str, int]:
    """Audit persisted TAM rows and optionally remove proven bad rows.

    Removed rows are archived in the append-only coverage ledger before deletion,
    so the rejected value, source, and reason remain recoverable for audit.
    """
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"total": 0, "valid": 0, "invalid": 0, "deleted": 0, "archived": 0}
    with session_scope() as session:
        rows = session.query(MarketOpportunityEstimate).order_by(MarketOpportunityEstimate.id).all()
        counts["total"] = len(rows)
        for row in rows:
            raw_input = dict((row.raw_payload or {}).get("input") or {})
            raw_input.setdefault("tam_value", float(row.tam_value) if row.tam_value is not None else None)
            raw_input.setdefault(
                "current_revenue_addressable",
                float(row.current_revenue_addressable) if row.current_revenue_addressable is not None else None,
            )
            raw_input.setdefault("currency", row.currency)
            raw_input.setdefault("confidence", row.confidence)
            machine = row.created_by == "machine" or row.source == "sec_edgar"
            validation = validate_market_opportunity(raw_input, machine_extracted=machine)
            if validation.valid and row.source == "sec_edgar":
                match = _TAM_RE.search(row.source_excerpt or "")
                if match is not None and match.group(2) is None:
                    validation = MarketOpportunityValidation(False, "sec_tam_scale_missing", None, "low")
            if validation.valid:
                counts["valid"] += 1
                continue
            counts["invalid"] += 1
            if not apply:
                continue
            archive_observed_at = observed_at + datetime.timedelta(microseconds=row.id)
            session.add(LiveDatasetCoverage(
                ticker_id=row.ticker_id, dataset="market_opportunity", observed_at=archive_observed_at,
                attempted_at=archive_observed_at, source="tam_revalidation", source_url=row.source_url,
                source_scope="Phase 0 existing-row quality audit", coverage_status=CoverageStatus.COLLECTION_FAILED,
                reason_code=CoverageReasonCode.PARSE_ERROR, reason_detail=validation.reason,
                retryable=False, confidence="high", raw_payload={"rejected_estimate": {
                    "id": row.id, "as_of": row.as_of.isoformat(), "method": row.method,
                    "tam_value": str(row.tam_value), "currency": row.currency,
                    "source": row.source, "source_excerpt": row.source_excerpt,
                    "original_raw_payload": row.raw_payload,
                }},
            ))
            session.query(MarketOpportunityComponent).filter_by(estimate_id=row.id).delete()
            session.delete(row)
            counts["deleted"] += 1
            counts["archived"] += 1
        if not apply:
            session.rollback()
    return counts


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
