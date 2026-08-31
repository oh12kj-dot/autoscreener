"""Idempotent SEC-section extraction for TENX v2 display data."""

from __future__ import annotations

import datetime

from autoscreener.db.models import (
    CapitalAllocationEvent, DebtInstrument, FilingSection, Guidance,
    ManagementGuidanceSnapshot, ManagementIncentiveSnapshot,
    OperatingKpiDefinition, OperatingKpiObservation, Ticker,
    ThesisMilestone,
    LiveDatasetCoverage,
)
from autoscreener.db.session import session_scope
from autoscreener.screening.investment_intelligence_extract import (
    extract_beneficial_ownership, extract_capital_events, extract_debt_maturities,
    extract_operating_kpis,
)
from autoscreener.research.notes import load_all_notes


_KPI_LABELS = {
    "arr": ("Annual recurring revenue", "USD", "general_corporate"),
    "nrr": ("Net revenue retention", "ratio", "general_corporate"),
    "backlog": ("Backlog", "USD", "general_corporate"),
    "customer_count": ("Customer count", "count", "general_corporate"),
}


def collect_investment_intelligence(*, symbols: list[str] | None = None,
                                    observed_at: datetime.datetime | None = None) -> dict[str, int]:
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"sections": 0, "kpis": 0, "debt": 0, "capital_events": 0, "incentives": 0, "guidance": 0, "milestones": 0}
    with session_scope() as session:
        definitions = {}
        for code, (label, unit, family) in _KPI_LABELS.items():
            row = session.query(OperatingKpiDefinition).filter_by(code=code).one_or_none()
            if row is None:
                row = OperatingKpiDefinition(code=code, label=label, unit=unit, model_family=family,
                                             description="Company-defined metric; verify source excerpt.")
                session.add(row); session.flush()
            definitions[code] = row

        query = session.query(FilingSection)
        if symbols:
            ids = [id_ for (id_,) in session.query(Ticker.id).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()]
            query = query.filter(FilingSection.ticker_id.in_(ids))
        section_rows = query.order_by(FilingSection.filed_date.asc()).all()
        processed_ticker_ids = {section.ticker_id for section in section_rows}
        for section in section_rows:
            counts["sections"] += 1
            source = "sec_edgar"
            for item in extract_operating_kpis(section.text):
                definition = definitions[item.code]
                exists = session.query(OperatingKpiObservation.id).filter_by(
                    ticker_id=section.ticker_id, kpi_definition_id=definition.id,
                    period_end=section.filed_date, source_accession=section.accession_number,
                ).first()
                if exists: continue
                session.add(OperatingKpiObservation(ticker_id=section.ticker_id,kpi_definition_id=definition.id,
                    period_end=section.filed_date,reported_at=observed_at,observed_at=observed_at,value=item.value,
                    company_definition=item.excerpt,source=source,source_accession=section.accession_number,
                    source_url=section.source_url,source_excerpt=item.excerpt,extraction_method="regex",confidence="medium",
                    coverage_status="collected_with_data")); counts["kpis"] += 1
            for index, item in enumerate(extract_debt_maturities(section.text)):
                instrument_id = f"{section.accession_number}:{item.maturity_year}:{index}"
                if session.query(DebtInstrument.id).filter_by(ticker_id=section.ticker_id,instrument_id=instrument_id,as_of=section.filed_date).first(): continue
                session.add(DebtInstrument(ticker_id=section.ticker_id,instrument_id=instrument_id,as_of=section.filed_date,
                    observed_at=observed_at,instrument_type="disclosed_debt",principal=item.principal,currency="USD",
                    maturity_date=datetime.date(item.maturity_year,12,31),covenant_summary=item.excerpt,source=source,
                    source_accession=section.accession_number,source_url=section.source_url,raw_payload={"excerpt":item.excerpt},
                    coverage_status="collected_with_data",confidence="medium")); counts["debt"] += 1
            for index, item in enumerate(extract_capital_events(section.text)):
                exists = session.query(CapitalAllocationEvent.id).filter_by(ticker_id=section.ticker_id,
                    source_accession=section.accession_number,event_type=item.event_type).first()
                if exists: continue
                session.add(CapitalAllocationEvent(ticker_id=section.ticker_id,announced_at=datetime.datetime.combine(section.filed_date,datetime.time.min,tzinfo=datetime.timezone.utc),
                    observed_at=observed_at,event_type=item.event_type,amount=item.amount,currency="USD",counterparty_or_asset=item.excerpt,
                    source=source,source_accession=section.accession_number,source_url=section.source_url,
                    coverage_status="collected_with_data",confidence="medium")); counts["capital_events"] += 1
            if section.form.upper().replace(" ", "") in {"DEF14A", "DEF14A/A"}:
                ownership = extract_beneficial_ownership(section.text)
                if ownership is not None and not session.query(ManagementIncentiveSnapshot.id).filter_by(
                    ticker_id=section.ticker_id,source_accession=section.accession_number).first():
                    session.add(ManagementIncentiveSnapshot(ticker_id=section.ticker_id,proxy_date=section.filed_date,
                        observed_at=observed_at,executive_name="Named executive officers (aggregate disclosure)",
                        beneficial_ownership_pct=ownership,source=source,source_accession=section.accession_number,
                        source_url=section.source_url,coverage_status="collected_with_data",confidence="low")); counts["incentives"] += 1

        # Existing guidance extraction becomes an append-only PIT history.
        for row in session.query(Guidance).order_by(Guidance.filed_date.asc()).all():
            announced = datetime.datetime.combine(row.filed_date, datetime.time.min, tzinfo=datetime.timezone.utc)
            if session.query(ManagementGuidanceSnapshot.id).filter_by(ticker_id=row.ticker_id,
                source_accession=row.accession_number,metric=row.metric).first(): continue
            session.add(ManagementGuidanceSnapshot(ticker_id=row.ticker_id,announced_at=announced,observed_at=observed_at,
                metric=row.metric,low=float(row.low_usd) if row.low_usd is not None else None,
                high=float(row.high_usd) if row.high_usd is not None else None,unit="USD",status="initiated",
                source="sec_edgar",source_accession=row.accession_number,raw_payload={"period_label":row.period_label,"raw_text":row.raw_text},
                coverage_status="collected_with_data",confidence="medium")); counts["guidance"] += 1

        for symbol, note in load_all_notes().items():
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker is None:
                continue
            for item in note.front_matter.get("milestones") or []:
                if not isinstance(item, dict) or not item.get("due_date") or not item.get("metric"):
                    continue
                due_date = item["due_date"]
                if isinstance(due_date, str):
                    due_date = datetime.date.fromisoformat(due_date)
                if session.query(ThesisMilestone.id).filter_by(
                    ticker_id=ticker.id, due_date=due_date, metric_code=str(item["metric"])
                ).first():
                    continue
                session.add(ThesisMilestone(
                    ticker_id=ticker.id, observed_at=observed_at, due_date=due_date,
                    category=str(item.get("category") or "other"), metric_code=str(item["metric"]),
                    bull_threshold=item.get("bull"), base_threshold=item.get("base"),
                    bear_threshold=item.get("bear"), unit=item.get("unit"), source="user",
                    status="pending", raw_payload=item, coverage_status="collected_with_data",
                    confidence="manual",
                ))
                counts["milestones"] += 1

        coverage_models = {
            "operating_kpis": OperatingKpiObservation,
            "debt_profile": DebtInstrument,
            "capital_allocation": CapitalAllocationEvent,
            "management_incentives": ManagementIncentiveSnapshot,
        }
        for ticker_id in processed_ticker_ids:
            for dataset, model in coverage_models.items():
                has_data = session.query(model.id).filter(model.ticker_id == ticker_id).first() is not None
                status = "collected_with_data" if has_data else "collected_no_finding"
                latest = session.query(LiveDatasetCoverage).filter_by(
                    ticker_id=ticker_id, dataset=dataset, source="sec_edgar"
                ).order_by(LiveDatasetCoverage.observed_at.desc()).first()
                if latest is not None and latest.coverage_status == status:
                    continue
                session.add(LiveDatasetCoverage(
                    ticker_id=ticker_id, dataset=dataset, observed_at=observed_at,
                    source="sec_edgar", coverage_status=status, confidence="medium",
                ))
    return counts
