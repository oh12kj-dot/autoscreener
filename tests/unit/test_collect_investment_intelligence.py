"""Investment intelligence collection regression tests."""

from __future__ import annotations

import datetime

import pytest

from autoscreener.batch.collect_investment_intelligence import collect_investment_intelligence
from autoscreener.dates import utc_today
from autoscreener.db.models import (
    CapitalAllocationEvent,
    DebtInstrument,
    FilingSection,
    LiveDatasetCoverage,
    ManagementIncentiveSnapshot,
    OperatingKpiObservation,
    Ticker,
)
from autoscreener.db.session import session_scope


_SYMBOL = "ZZINT9"


def _cleanup() -> None:
    with session_scope() as session:
        ticker = session.query(Ticker).filter_by(symbol=_SYMBOL).one_or_none()
        if ticker is None:
            return
        for model in (
            OperatingKpiObservation,
            DebtInstrument,
            CapitalAllocationEvent,
            ManagementIncentiveSnapshot,
            LiveDatasetCoverage,
            FilingSection,
        ):
            session.query(model).filter_by(ticker_id=ticker.id).delete()
        session.delete(ticker)


@pytest.fixture
def ticker_id():
    _cleanup()
    with session_scope() as session:
        ticker = Ticker(symbol=_SYMBOL, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
    yield ticker_id
    _cleanup()


def test_same_day_sections_do_not_duplicate_kpi_unique_key(ticker_id: int) -> None:
    filed_date = datetime.date(2026, 2, 26)
    with session_scope() as session:
        for index, value in enumerate(("20.6", "21.0"), start=1):
            text = f"Backlog as of December 31, 2025 was ${value} billion."
            session.add(
                FilingSection(
                    ticker_id=ticker_id,
                    accession_number=f"0000000001-26-00000{index}",
                    form="10-K",
                    filed_date=filed_date,
                    section=f"item{index}",
                    text=text,
                    char_count=len(text),
                    source_url=f"https://www.sec.gov/Archives/test-{index}.htm",
                    extracted_on=utc_today(),
                )
            )

    first_observed_at = datetime.datetime(2026, 9, 1, 4, 16, tzinfo=datetime.timezone.utc)
    first = collect_investment_intelligence(
        symbols=[_SYMBOL], observed_at=first_observed_at
    )
    second = collect_investment_intelligence(
        symbols=[_SYMBOL],
        observed_at=first_observed_at + datetime.timedelta(minutes=1),
    )

    assert first["sections"] == 2
    assert first["kpis"] == 1
    assert second["kpis"] == 0
    with session_scope() as session:
        rows = session.query(OperatingKpiObservation).filter_by(ticker_id=ticker_id).all()
    assert len(rows) == 1
    assert rows[0].reported_at == first_observed_at
