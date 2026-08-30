"""tests/unit/test_red_flags.py(30.4.5)。"""

from __future__ import annotations

import datetime

from autoscreener.screening.red_flags import (
    BLOCKING,
    WARNING,
    FilingView,
    evaluate_red_flags,
)

AS_OF = datetime.date(2026, 8, 28)


def _filing(**kwargs) -> FilingView:
    defaults = dict(
        accession_number="0001234567-26-000001",
        form="8-K",
        filed_date=AS_OF,
        items=None,
        document_url="https://www.sec.gov/Archives/edgar/data/1/000123456726000001/",
        analysis=None,
    )
    defaults.update(kwargs)
    return FilingView(**defaults)


def test_8k_item_402_is_restatement_blocking():
    flags = evaluate_red_flags([_filing(items=["4.02"])], as_of=AS_OF)
    assert len(flags) == 1
    assert flags[0].code == "restatement"
    assert flags[0].severity == BLOCKING


def test_nt_10q_beyond_ttl_is_not_returned():
    old_date = AS_OF - datetime.timedelta(days=200)
    flags = evaluate_red_flags([_filing(form="NT 10-Q", filed_date=old_date, items=[])], as_of=AS_OF)
    assert flags == []


def test_nt_10q_within_ttl_is_returned():
    recent_date = AS_OF - datetime.timedelta(days=100)
    flags = evaluate_red_flags([_filing(form="NT 10-Q", filed_date=recent_date, items=[])], as_of=AS_OF)
    assert len(flags) == 1
    assert flags[0].code == "late_filing"
    assert flags[0].severity == BLOCKING


def test_no_filings_returns_empty_list():
    assert evaluate_red_flags([], as_of=AS_OF) == []


def test_multiple_items_in_one_filing_produce_multiple_flags():
    flags = evaluate_red_flags([_filing(items=["4.02", "5.02"])], as_of=AS_OF)
    codes = sorted(f.code for f in flags)
    assert codes == ["officer_departure", "restatement"]


def test_going_concern_from_latest_10k_analysis():
    filings = [
        _filing(
            form="10-K",
            accession_number="acc-1",
            filed_date=AS_OF - datetime.timedelta(days=30),
            analysis={"going_concern": True, "material_weakness": False, "excerpt": "substantial doubt..."},
        )
    ]
    flags = evaluate_red_flags(filings, as_of=AS_OF)
    assert any(f.code == "going_concern" and f.severity == BLOCKING for f in flags)


def test_material_weakness_is_warning_not_blocking():
    filings = [
        _filing(
            form="10-K",
            accession_number="acc-1",
            filed_date=AS_OF - datetime.timedelta(days=30),
            analysis={"going_concern": False, "material_weakness": True, "excerpt": "..."},
        )
    ]
    flags = evaluate_red_flags(filings, as_of=AS_OF)
    mw = [f for f in flags if f.code == "material_weakness"]
    assert len(mw) == 1
    assert mw[0].severity == WARNING


def test_only_latest_10k_10q_analysis_is_used():
    """古い10-Kでgoing concernがあっても、新しい10-Qで解消していれば出ない。"""
    filings = [
        _filing(
            form="10-K",
            accession_number="old",
            filed_date=AS_OF - datetime.timedelta(days=200),
            analysis={"going_concern": True, "material_weakness": False, "excerpt": "..."},
        ),
        _filing(
            form="10-Q",
            accession_number="new",
            filed_date=AS_OF - datetime.timedelta(days=10),
            analysis={"going_concern": False, "material_weakness": False, "excerpt": ""},
        ),
    ]
    flags = evaluate_red_flags(filings, as_of=AS_OF)
    assert not any(f.code == "going_concern" for f in flags)


def test_delisting_form_is_blocking():
    flags = evaluate_red_flags([_filing(form="25-NSE", items=[])], as_of=AS_OF)
    assert flags[0].code == "delisting_form"
    assert flags[0].severity == BLOCKING


def test_flags_sorted_newest_first():
    filings = [
        _filing(accession_number="a", filed_date=AS_OF - datetime.timedelta(days=100), items=["4.02"]),
        _filing(accession_number="b", filed_date=AS_OF - datetime.timedelta(days=5), items=["1.01"]),
    ]
    flags = evaluate_red_flags(filings, as_of=AS_OF)
    assert flags[0].source_accession == "b"
    assert flags[1].source_accession == "a"


def test_document_url_always_present():
    flags = evaluate_red_flags([_filing(items=["4.02"])], as_of=AS_OF)
    assert flags[0].document_url is not None
