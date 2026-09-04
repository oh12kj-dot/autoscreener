"""tests/unit/test_edgar_client.py(30.3.7)。"""

from __future__ import annotations

import datetime
import time

import pytest
import responses

from autoscreener.collectors.edgar_client import (
    COMPANY_TICKERS_URL,
    DAILY_INDEX_MASTER_URL,
    SUBMISSIONS_URL,
    EdgarClient,
    RateLimiter,
)
from autoscreener.collectors.errors import ParseFailure, PermanentFailure
from autoscreener.config import EdgarRetryConfig, EdgarConfig


def _config(**overrides) -> EdgarConfig:
    defaults = dict(
        enabled=True,
        requests_per_second=10.0,  # 上限いっぱい(EdgarConfigはle=10)。テストが遅くなりすぎない値
        timeout_seconds=5.0,
        document_fetch_enabled=True,
        max_tracked_tickers=300,
        retry=EdgarRetryConfig(max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.02),
    )
    defaults.update(overrides)
    return EdgarConfig(**defaults)


def test_missing_user_agent_raises_value_error():
    with pytest.raises(ValueError, match="EDGAR_USER_AGENT"):
        EdgarClient(_config(), "")


def test_placeholder_user_agent_raises_value_error():
    with pytest.raises(ValueError):
        EdgarClient(_config(), "your-address@example.com")


def test_rate_limiter_enforces_minimum_interval():
    limiter = RateLimiter(requests_per_second=50.0)  # min interval 0.02s
    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # 9 intervals of 0.02s = 0.18s の下限(初回はwaitしない)
    assert elapsed >= 0.15


@responses.activate
def test_fetch_company_tickers_normalizes_symbols():
    responses.add(
        responses.GET,
        COMPANY_TICKERS_URL,
        json={
            "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
            "1": {"cik_str": 1234567, "ticker": "brk.b", "title": "Berkshire"},
        },
        status=200,
    )
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    result = client.fetch_company_tickers()
    assert result["AAPL"] == "0000320193"
    assert result["BRK.B"] == "0001234567"


@responses.activate
def test_fetch_filings_parses_parallel_arrays_without_index_mistakes():
    responses.add(
        responses.GET,
        SUBMISSIONS_URL.format(cik="0000320193"),
        json={
            "filings": {
                "recent": {
                    "accessionNumber": ["0001234567-26-000001", "0001234567-26-000002"],
                    "form": ["8-K", "10-K"],
                    "filingDate": ["2026-08-01", "2026-08-15"],
                    "reportDate": ["2026-07-31", None],
                    "items": ["Item 2.02: Results of Operations and Financial Condition", ""],
                    "primaryDocument": ["form8k.htm", "form10k.htm"],
                }
            }
        },
        status=200,
    )
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    records = client.fetch_filings("0000320193")
    assert len(records) == 2
    assert records[0].form == "8-K"
    assert records[0].items == ["2.02"]
    assert records[0].document_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000123456726000001/form8k.htm"
    )
    assert records[1].form == "10-K"
    assert records[1].report_date is None
    assert records[1].items == []


@responses.activate
def test_fetch_filings_filters_by_forms():
    responses.add(
        responses.GET,
        SUBMISSIONS_URL.format(cik="0000320193"),
        json={
            "filings": {
                "recent": {
                    "accessionNumber": ["a", "b"],
                    "form": ["8-K", "10-K"],
                    "filingDate": ["2026-08-01", "2026-08-15"],
                    "reportDate": [None, None],
                    "items": ["", ""],
                    "primaryDocument": ["a.htm", "b.htm"],
                }
            }
        },
        status=200,
    )
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    records = client.fetch_filings("0000320193", forms={"10-K"})
    assert len(records) == 1
    assert records[0].form == "10-K"


@responses.activate
def test_fetch_daily_index_returns_only_requested_form_ciks():
    filing_date = datetime.date(2026, 9, 3)
    url = DAILY_INDEX_MASTER_URL.format(year=2026, quarter=3, stamp="20260903")
    responses.add(
        responses.GET,
        url,
        body=(
            "CIK|Company Name|Form Type|Date Filed|Filename\n"
            "320193|APPLE INC|8-K|2026-09-03|edgar/data/320193/a.txt\n"
            "789019|MICROSOFT CORP|4|2026-09-03|edgar/data/789019/b.txt\n"
        ),
        status=200,
    )
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    assert client.fetch_daily_index_ciks(filing_date, forms={"8-K"}) == {"0000320193"}


@responses.activate
def test_fetch_daily_index_rejects_unexpected_success_page():
    filing_date = datetime.date(2026, 9, 3)
    url = DAILY_INDEX_MASTER_URL.format(year=2026, quarter=3, stamp="20260903")
    responses.add(responses.GET, url, body="<html>temporary page</html>", status=200)
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    with pytest.raises(ParseFailure, match="unexpected format"):
        client.fetch_daily_index_ciks(filing_date)


@responses.activate
def test_http_403_is_permanent_failure_not_retried():
    call_count = {"n": 0}

    def _callback(request):
        call_count["n"] += 1
        return (403, {}, "forbidden")

    responses.add_callback(responses.GET, COMPANY_TICKERS_URL, callback=_callback)
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    with pytest.raises(PermanentFailure):
        client.fetch_company_tickers()
    assert call_count["n"] == 1  # リトライされていない


@responses.activate
def test_http_404_is_empty_response_error():
    from autoscreener.collectors.errors import EmptyResponseError

    responses.add(responses.GET, SUBMISSIONS_URL.format(cik="0000000001"), status=404)
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    with pytest.raises(EmptyResponseError):
        client.fetch_filings("0000000001")


@responses.activate
def test_mismatched_parallel_array_lengths_raise_parse_failure():
    responses.add(
        responses.GET,
        SUBMISSIONS_URL.format(cik="0000320193"),
        json={
            "filings": {
                "recent": {
                    "accessionNumber": ["a", "b"],
                    "form": ["8-K"],  # 意図的に長さを合わせない
                    "filingDate": ["2026-08-01", "2026-08-15"],
                }
            }
        },
        status=200,
    )
    client = EdgarClient(_config(), "TENX research <test@example.com>")
    with pytest.raises(ParseFailure):
        client.fetch_filings("0000320193")
