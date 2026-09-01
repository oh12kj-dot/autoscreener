from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from autoscreener.api.schemas import InvestmentIntelligenceResponse
from autoscreener.batch.collect_filing_sections import foreign_form_section_names
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import LiveDatasetCoverage, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.investment_intelligence import macro_exposure
from autoscreener.screening.investment_intelligence_extract import extract_capital_events


def test_coverage_response_accepts_only_the_shared_status_contract() -> None:
    response = InvestmentIntelligenceResponse(ticker="TEST", as_of=datetime.date(2026, 9, 1), coverage_status=CoverageStatus.COLLECTION_FAILED, reason_code="provider_error")
    assert response.coverage_status is CoverageStatus.COLLECTION_FAILED
    with pytest.raises(ValidationError):
        InvestmentIntelligenceResponse(ticker="TEST", as_of=datetime.date(2026, 9, 1), coverage_status="unknown")


def test_foreign_form_mapping_does_not_treat_20f_as_no_supported_filing() -> None:
    assert foreign_form_section_names("20-F") == {"foreign_annual"}
    assert foreign_form_section_names("6-K") == {"foreign_report"}


def test_capital_events_keep_multiple_evidence_rows() -> None:
    events = extract_capital_events("We completed a $10 million repurchase and a $20 million acquisition.")
    assert [(item.event_type, item.amount) for item in events] == [("buyback", 10_000_000), ("acquisition", 20_000_000)]


def test_macro_exposure_keeps_missing_and_zero_variance_distinct() -> None:
    result = macro_exposure([0.01, 0.02, 0.03], [0.0, 0.0, 0.0])
    assert result["beta"] is None
    assert result["sample_count"] == 3


def test_database_rejects_an_unknown_coverage_status() -> None:
    symbol = "LICOVERAGE"
    with pytest.raises(IntegrityError):
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if ticker is None:
                ticker = Ticker(symbol=symbol, market="US")
                session.add(ticker)
                session.flush()
            session.add(LiveDatasetCoverage(ticker_id=ticker.id, dataset="test", observed_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
                source="test", coverage_status="invalid", confidence="low"))
