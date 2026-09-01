"""Idempotent SEC-section extraction for TENX v2 display data."""

from __future__ import annotations

import datetime
import hashlib

from autoscreener.db.models import (
    CapitalAllocationEvent, DebtInstrument, FilingSection, Guidance, LiquidityFacility,
    ManagementGuidanceSnapshot, ManagementIncentiveSnapshot,
    OperatingKpiDefinition, OperatingKpiObservation, Ticker,
    ThesisMilestone,
    LiveDatasetCoverage,
)
from autoscreener.db.session import session_scope
from autoscreener.coverage import CoverageReasonCode, CoverageStatus
from autoscreener.screening.investment_intelligence_extract import (
    extract_beneficial_ownership, extract_capital_events, extract_debt_maturities,
    extract_liquidity_facilities, extract_management_incentives, extract_operating_kpis,
)
from autoscreener.research.notes import load_all_notes


_KPI_LABELS = {
    "arr": ("Annual recurring revenue", "USD", "general_corporate"),
    "nrr": ("Net revenue retention", "ratio", "general_corporate"),
    "backlog": ("Backlog", "USD", "general_corporate"),
    "customer_count": ("Customer count", "count", "general_corporate"),
    "rpo": ("Remaining performance obligations", "USD", "saas"),
    "gmv": ("Gross merchandise value", "USD", "marketplace"),
    "tpv": ("Total payment volume", "USD", "marketplace"),
    "take_rate": ("Take rate", "ratio", "marketplace"),
    "store_count": ("Store count", "count", "consumer"),
    "book_to_bill": ("Book-to-bill", "ratio", "industrial"),
    "production": ("Production", "count", "mining_energy"),
}


def collect_investment_intelligence(*, symbols: list[str] | None = None,
                                    observed_at: datetime.datetime | None = None) -> dict[str, int]:
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    counts = {"targets": 0, "succeeded": 0, "no_finding": 0, "failed": 0, "outside_scope": 0,
              "sections": 0, "kpis": 0, "debt": 0, "capital_events": 0, "liquidity": 0,
              "incentives": 0, "guidance": 0, "milestones": 0, "rows_written": 0}
    with session_scope() as session:
        definitions = {}
        for code, (label, unit, family) in _KPI_LABELS.items():
            row = session.query(OperatingKpiDefinition).filter_by(code=code).one_or_none()
            if row is None:
                row = OperatingKpiDefinition(code=code, label=label, unit=unit, model_family=family,
                                             description="Company-defined metric; verify source excerpt.")
                session.add(row); session.flush()
            definitions[code] = row

        normalized_symbols = {symbol.upper() for symbol in symbols} if symbols else None
        target_query = session.query(Ticker)
        if normalized_symbols is not None:
            target_query = target_query.filter(Ticker.symbol.in_(normalized_symbols))
        else:
            from autoscreener.batch.collect_filings import select_tracked_tickers
            from autoscreener.config import load_edgar_config
            target_ids = [ticker.id for ticker in select_tracked_tickers(session, limit=load_edgar_config().max_tracked_tickers)]
            target_query = target_query.filter(Ticker.id.in_(target_ids))
        targets = target_query.order_by(Ticker.symbol).all()
        target_ids = {ticker.id for ticker in targets}
        counts["targets"] = len(targets)
        section_rows = session.query(FilingSection).filter(FilingSection.ticker_id.in_(target_ids)).order_by(
            FilingSection.ticker_id, FilingSection.filed_date.asc(), FilingSection.id.asc()
        ).all() if target_ids else []
        sections_by_ticker: dict[int, list[FilingSection]] = {ticker.id: [] for ticker in targets}
        for section in section_rows:
            sections_by_ticker.setdefault(section.ticker_id, []).append(section)
        # uq_kpi_observation は source_accession ではなくこの4列を一意キーにする。
        # 同一日に提出された複数section/accessionから同じKPIが抽出される場合、
        # DB照会だけでは同じトランザクション内の未flush行を判定できず、後続照会の
        # autoflushでUniqueViolationになる。DBの一意キーと同じキーを実行内でも
        # 予約し、最初の根拠行だけを採用する。
        seen_kpi_observation_keys: set[tuple[int, int, datetime.date, datetime.datetime]] = set()
        coverage_models = {
            "operating_kpis": OperatingKpiObservation,
            "debt_profile": DebtInstrument,
            "capital_allocation": CapitalAllocationEvent,
            "management_incentives": ManagementIncentiveSnapshot,
        }
        for ticker in targets:
            ticker_sections = sections_by_ticker[ticker.id]
            if not ticker_sections:
                for dataset in coverage_models:
                    _record_coverage(session, ticker.id, dataset, observed_at, CoverageStatus.NOT_COLLECTED,
                        reason_code=CoverageReasonCode.NO_SUPPORTED_FILING, source_scope="10-k,item7;10-q,item7;8-k;def14a;20-f;6-k")
                counts["no_finding"] += 1
                continue
            try:
                with session.begin_nested():
                    for section in ticker_sections:
                        counts["sections"] += 1
                        _extract_section(session, section, definitions, seen_kpi_observation_keys, observed_at, counts)
                for dataset, model in coverage_models.items():
                    has_data = session.query(model.id).filter(model.ticker_id == ticker.id).first() is not None
                    if dataset == "debt_profile" and not has_data:
                        has_data = session.query(LiquidityFacility.id).filter(LiquidityFacility.ticker_id == ticker.id).first() is not None
                    _record_coverage(session, ticker.id, dataset, observed_at,
                        CoverageStatus.COLLECTED_WITH_DATA if has_data else CoverageStatus.COLLECTED_NO_FINDING,
                        reason_code=None if has_data else CoverageReasonCode.SOURCE_SCANNED_NO_MATCH,
                        source_scope="10-k,item7;10-q,item7;8-k;def14a;20-f;6-k")
                counts["succeeded"] += 1
            except Exception as exc:
                for dataset in coverage_models:
                    _record_coverage(session, ticker.id, dataset, observed_at, CoverageStatus.COLLECTION_FAILED,
                        reason_code=CoverageReasonCode.PARSE_ERROR, reason_detail=type(exc).__name__, retryable=False,
                        source_scope="sec filing sections")
                counts["failed"] += 1

        # Existing guidance extraction becomes an append-only PIT history.
        guidance_query = session.query(Guidance)
        if target_ids:
            guidance_query = guidance_query.filter(Guidance.ticker_id.in_(target_ids))
        for row in guidance_query.order_by(Guidance.filed_date.asc()).all():
            announced = datetime.datetime.combine(row.filed_date, datetime.time.min, tzinfo=datetime.timezone.utc)
            if session.query(ManagementGuidanceSnapshot.id).filter_by(ticker_id=row.ticker_id,
                source_accession=row.accession_number,metric=row.metric).first(): continue
            session.add(ManagementGuidanceSnapshot(ticker_id=row.ticker_id,announced_at=announced,observed_at=observed_at,
                metric=row.metric,low=float(row.low_usd) if row.low_usd is not None else None,
                high=float(row.high_usd) if row.high_usd is not None else None,unit="USD",status="initiated",
                source="sec_edgar",source_accession=row.accession_number,raw_payload={"period_label":row.period_label,"raw_text":row.raw_text},
                coverage_status=CoverageStatus.COLLECTED_WITH_DATA,confidence="medium")); counts["guidance"] += 1

        notes = load_all_notes()
        for ticker in targets:
            note = notes.get(ticker.symbol)
            if note is None:
                _record_coverage(session, ticker.id, "thesis_milestones", observed_at, CoverageStatus.NOT_COLLECTED,
                    source="user", reason_code=CoverageReasonCode.USER_INPUT_MISSING, source_scope="research/<TICKER>.md")
                continue
            milestones = note.front_matter.get("milestones")
            if milestones is None:
                _record_coverage(session, ticker.id, "thesis_milestones", observed_at, CoverageStatus.COLLECTED_NO_FINDING,
                    source="user", reason_code=CoverageReasonCode.SOURCE_SCANNED_NO_MATCH, source_scope=str(note.path))
                continue
            for item in milestones if isinstance(milestones, list) else []:
                if not isinstance(item, dict) or not item.get("due_date") or not item.get("metric"):
                    continue
                due_date = item["due_date"]
                if isinstance(due_date, str): due_date = datetime.date.fromisoformat(due_date)
                if session.query(ThesisMilestone.id).filter_by(ticker_id=ticker.id, due_date=due_date, metric_code=str(item["metric"])).first(): continue
                session.add(ThesisMilestone(ticker_id=ticker.id, observed_at=observed_at, due_date=due_date,
                    category=str(item.get("category") or "other"), metric_code=str(item["metric"]), bull_threshold=item.get("bull"),
                    base_threshold=item.get("base"), bear_threshold=item.get("bear"), unit=item.get("unit"), source="user", status="pending",
                    raw_payload=item, coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="manual")); counts["milestones"] += 1
            _record_coverage(session, ticker.id, "thesis_milestones", observed_at, CoverageStatus.COLLECTED_WITH_DATA if milestones else CoverageStatus.COLLECTED_NO_FINDING,
                source="user", reason_code=None if milestones else CoverageReasonCode.SOURCE_SCANNED_NO_MATCH, source_scope=str(note.path))
        counts["rows_written"] = sum(counts[key] for key in ("kpis", "debt", "capital_events", "liquidity", "incentives", "guidance", "milestones"))
    return counts


def _extract_section(session, section: FilingSection, definitions, seen_kpi_observation_keys, observed_at, counts: dict[str, int]) -> None:
    source = "sec_edgar"
    for item in extract_operating_kpis(section.text):
        definition = definitions[item.code]
        observation_key = (section.ticker_id, definition.id, section.filed_date, observed_at)
        if observation_key in seen_kpi_observation_keys: continue
        exists = session.query(OperatingKpiObservation.id).filter_by(ticker_id=section.ticker_id, kpi_definition_id=definition.id, period_end=section.filed_date).first()
        if exists:
            seen_kpi_observation_keys.add(observation_key); continue
        session.add(OperatingKpiObservation(ticker_id=section.ticker_id,kpi_definition_id=definition.id, period_end=section.filed_date,
            reported_at=observed_at,observed_at=observed_at,value=item.value,company_definition=item.excerpt,source=source,
            source_accession=section.accession_number,source_url=section.source_url,source_excerpt=item.excerpt,extraction_method="regex",
            confidence="medium",coverage_status=CoverageStatus.COLLECTED_WITH_DATA))
        seen_kpi_observation_keys.add(observation_key); counts["kpis"] += 1
    for index, item in enumerate(extract_debt_maturities(section.text)):
        instrument_id = f"{section.accession_number}:{item.maturity_year}:{index}"
        if session.query(DebtInstrument.id).filter_by(ticker_id=section.ticker_id,instrument_id=instrument_id,as_of=section.filed_date).first(): continue
        session.add(DebtInstrument(ticker_id=section.ticker_id,instrument_id=instrument_id,as_of=section.filed_date,observed_at=observed_at,
            instrument_type="disclosed_debt",principal=item.principal,currency="USD",maturity_date=datetime.date(item.maturity_year,12,31),
            covenant_summary=item.excerpt,source=source,source_accession=section.accession_number,source_url=section.source_url,
            raw_payload={"excerpt":item.excerpt},coverage_status=CoverageStatus.COLLECTED_WITH_DATA,confidence="medium")); counts["debt"] += 1
    for item in extract_capital_events(section.text):
        digest = hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest()
        if session.query(CapitalAllocationEvent.id).filter_by(ticker_id=section.ticker_id, source_accession=section.accession_number, event_type=item.event_type, content_hash=digest).first(): continue
        session.add(CapitalAllocationEvent(ticker_id=section.ticker_id,announced_at=datetime.datetime.combine(section.filed_date,datetime.time.min,tzinfo=datetime.timezone.utc),
            observed_at=observed_at,event_type=item.event_type,amount=item.amount,currency="USD",counterparty_or_asset=item.excerpt,source=source,
            source_accession=section.accession_number,source_url=section.source_url,source_excerpt=item.excerpt,content_hash=digest,
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA,confidence="medium")); counts["capital_events"] += 1
    for index, item in enumerate(extract_liquidity_facilities(section.text)):
        if session.query(LiquidityFacility.id).filter_by(ticker_id=section.ticker_id,as_of=section.filed_date,source_accession=section.accession_number).first(): continue
        session.add(LiquidityFacility(ticker_id=section.ticker_id,as_of=section.filed_date,observed_at=observed_at,revolver_total=item.revolver_total,
            revolver_drawn=item.revolver_drawn,revolver_available=item.revolver_available,cash_balance=item.cash_balance,source=source,
            source_accession=section.accession_number,source_url=section.source_url,raw_payload={"excerpt": item.excerpt, "index": index},
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA,confidence="medium")); counts["liquidity"] += 1
    if section.form.upper().replace(" ", "") in {"DEF14A", "DEF14A/A", "20-F", "20-F/A"}:
        for item in extract_management_incentives(section.text):
            if session.query(ManagementIncentiveSnapshot.id).filter_by(
                ticker_id=section.ticker_id, source_accession=section.accession_number, executive_name=item.executive_name,
            ).first():
                continue
            session.add(ManagementIncentiveSnapshot(ticker_id=section.ticker_id,proxy_date=section.filed_date,observed_at=observed_at,
                executive_name=item.executive_name,role=item.role,beneficial_ownership_pct=item.ownership_pct,total_compensation=item.total_compensation,
                equity_compensation_pct=item.equity_compensation_pct,performance_metrics=[],source=source,source_accession=section.accession_number,
                source_url=section.source_url,raw_payload={"excerpt": item.excerpt},coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
                confidence="medium")); counts["incentives"] += 1
        ownership = extract_beneficial_ownership(section.text)
        if ownership is not None and not session.query(ManagementIncentiveSnapshot.id).filter_by(ticker_id=section.ticker_id,source_accession=section.accession_number).first():
            session.add(ManagementIncentiveSnapshot(ticker_id=section.ticker_id,proxy_date=section.filed_date,observed_at=observed_at,
                executive_name="Named executive officers (aggregate disclosure)",beneficial_ownership_pct=ownership,source=source,
                source_accession=section.accession_number,source_url=section.source_url,coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
                confidence="low")); counts["incentives"] += 1


def _record_coverage(session, ticker_id: int, dataset: str, observed_at: datetime.datetime, status: CoverageStatus, *,
                     source: str = "sec_edgar", reason_code: CoverageReasonCode | None = None,
                     reason_detail: str | None = None, source_scope: str | None = None, retryable: bool | None = None) -> None:
    session.add(LiveDatasetCoverage(ticker_id=ticker_id, dataset=dataset, observed_at=observed_at, source=source,
        coverage_status=status, confidence="medium", reason_code=reason_code, reason_detail=reason_detail,
        attempted_at=observed_at, source_scope=source_scope, retryable=retryable))
