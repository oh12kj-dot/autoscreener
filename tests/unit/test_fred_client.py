"""tests/unit/test_fred_client.py(30.8.4)。"""

from __future__ import annotations

import datetime

import pytest
import responses

from autoscreener.collectors.errors import EmptyResponseError, PermanentFailure
from autoscreener.collectors.fred_client import FRED_SERIES_OBSERVATIONS_URL, FredClient


def test_missing_api_key_raises_value_error():
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        FredClient("")


@responses.activate
def test_fetch_series_parses_observations():
    responses.add(
        responses.GET,
        FRED_SERIES_OBSERVATIONS_URL,
        json={
            "observations": [
                {"date": "2026-08-01", "value": "4.25"},
                {"date": "2026-08-02", "value": "."},  # 欠測
            ]
        },
        status=200,
    )
    client = FredClient("test-key", requests_per_second=100.0)
    result = client.fetch_series("DGS10")
    assert result[0].observation_date == datetime.date(2026, 8, 1)
    assert result[0].value == 4.25
    assert result[1].value is None


@responses.activate
def test_invalid_api_key_is_permanent_failure():
    responses.add(responses.GET, FRED_SERIES_OBSERVATIONS_URL, status=401)
    client = FredClient("bad-key", requests_per_second=100.0)
    with pytest.raises(PermanentFailure):
        client.fetch_series("DGS10")


@responses.activate
def test_unknown_series_is_empty_response_error():
    responses.add(responses.GET, FRED_SERIES_OBSERVATIONS_URL, status=400)
    client = FredClient("test-key", requests_per_second=100.0)
    with pytest.raises(EmptyResponseError):
        client.fetch_series("BOGUS_SERIES")
