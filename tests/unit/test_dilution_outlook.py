"""tests/unit/test_dilution_outlook.py(30.6.4)。"""

from __future__ import annotations

import datetime

from autoscreener.screening.dilution_outlook import (
    FilingRefView,
    NoteDilutionInputs,
    compute_dilution_outlook,
)

AS_OF = datetime.date(2026, 8, 28)


def _filing(form: str, days_ago: int, accession: str = "acc") -> FilingRefView:
    return FilingRefView(
        accession_number=accession,
        form=form,
        filed_date=AS_OF - datetime.timedelta(days=days_ago),
        document_url="https://www.sec.gov/x",
    )


def test_shelf_and_offering_filings_within_3_years_are_listed_by_date_desc():
    filings = [
        _filing("S-3", 100, "a"),
        _filing("424B5", 50, "b"),
        _filing("424B5", 400, "c"),
    ]
    result = compute_dilution_outlook(filings, as_of=AS_OF, historical_dilution_rate=0.06, market_cap=1e9)
    assert [f.accession_number for f in result.shelf_filings] == ["a"]
    assert [f.accession_number for f in result.offering_filings] == ["b", "c"]
    assert result.offerings_last_3y == 2


def test_filings_beyond_3_years_are_excluded():
    filings = [_filing("S-3", 365 * 4)]
    result = compute_dilution_outlook(filings, as_of=AS_OF, historical_dilution_rate=None, market_cap=1e9)
    assert result.shelf_filings == []


def test_no_note_input_leaves_reserved_ratio_none_not_zero():
    result = compute_dilution_outlook([], as_of=AS_OF, historical_dilution_rate=0.06, market_cap=1e9)
    assert result.remaining_shelf_capacity_usd is None
    assert result.reserved_dilution_ratio is None  # 「未入力」であって0ではない
    assert result.heavy_reserved_dilution is False


def test_reserved_dilution_ratio_computed_from_note():
    note = NoteDilutionInputs(remaining_shelf_capacity_usd=150_000_000, atm_remaining_usd=50_000_000)
    result = compute_dilution_outlook([], as_of=AS_OF, historical_dilution_rate=0.06, market_cap=1_000_000_000, note=note)
    assert result.reserved_dilution_ratio == 0.20


def test_heavy_reserved_dilution_flag_at_threshold():
    note = NoteDilutionInputs(remaining_shelf_capacity_usd=250_000_000)
    result = compute_dilution_outlook([], as_of=AS_OF, historical_dilution_rate=None, market_cap=1_000_000_000, note=note)
    assert result.reserved_dilution_ratio == 0.25
    assert result.heavy_reserved_dilution is True


def test_below_threshold_does_not_trigger_flag():
    note = NoteDilutionInputs(remaining_shelf_capacity_usd=100_000_000)
    result = compute_dilution_outlook([], as_of=AS_OF, historical_dilution_rate=None, market_cap=1_000_000_000, note=note)
    assert result.heavy_reserved_dilution is False
