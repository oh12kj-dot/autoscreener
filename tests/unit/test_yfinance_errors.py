import datetime
from unittest.mock import Mock

import pandas as pd
import pytest
import requests
from yfinance.exceptions import YFDataException, YFRateLimitError, YFTickerMissingError

from autoscreener.collectors.errors import (
    ParseFailure,
    PermanentFailure,
    TransientFailure,
    classify_exception,
    is_known_yfinance_session_typeerror,
)
from autoscreener.collectors.yfinance_client import fetch_latest_price
from autoscreener.config import RetryConfig


def test_rate_limit_error_is_transient():
    assert isinstance(classify_exception(YFRateLimitError()), TransientFailure)


def test_ticker_missing_error_is_permanent():
    assert isinstance(classify_exception(YFTickerMissingError("ZZZ", "no such ticker")), PermanentFailure)


def test_yf_data_exception_is_parse_failure():
    assert isinstance(classify_exception(YFDataException("unexpected shape")), ParseFailure)


def test_http_404_is_permanent():
    response = Mock(status_code=404)
    exc = requests.exceptions.HTTPError(response=response)
    assert isinstance(classify_exception(exc), PermanentFailure)


@pytest.mark.parametrize("status_code", [401, 429, 500, 502, 503, 504])
def test_http_5xx_and_429_and_401_are_transient(status_code):
    response = Mock(status_code=status_code)
    exc = requests.exceptions.HTTPError(response=response)
    assert isinstance(classify_exception(exc), TransientFailure)


def test_http_400_is_parse_failure_not_silently_retried():
    response = Mock(status_code=400)
    exc = requests.exceptions.HTTPError(response=response)
    assert isinstance(classify_exception(exc), ParseFailure)


def test_connection_error_is_transient():
    assert isinstance(classify_exception(requests.exceptions.ConnectionError("reset")), TransientFailure)


def test_timeout_is_transient():
    assert isinstance(classify_exception(requests.exceptions.Timeout("timed out")), TransientFailure)


def test_unknown_exception_is_parse_failure_not_swallowed():
    # 18.1: 未知の例外は「一時的失敗」に丸めず、仕様変更の疑いとして表面化させる
    assert isinstance(classify_exception(ValueError("totally unexpected")), ParseFailure)


def test_only_yfinance_stack_none_iterable_typeerror_is_retry_eligible():
    namespace: dict[str, object] = {}
    exec(compile("def raise_known():\n    raise TypeError(\"argument of type 'NoneType' is not iterable\")", "yfinance/data.py", "exec"), namespace)
    with pytest.raises(TypeError) as known:
        namespace["raise_known"]()
    assert is_known_yfinance_session_typeerror(known.value) is True

    with pytest.raises(TypeError) as unknown:
        raise TypeError("argument of type 'NoneType' is not iterable")
    assert is_known_yfinance_session_typeerror(unknown.value) is False


def test_latest_price_forces_share_refresh_when_split_is_present(monkeypatch):
    index = pd.DatetimeIndex([datetime.datetime(2026, 9, 3, tzinfo=datetime.UTC)])
    history = pd.DataFrame(
        {
            "Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5],
            "Volume": [1000], "Dividends": [0.0], "Stock Splits": [2.0],
        },
        index=index,
    )
    ticker = Mock()
    ticker.history.return_value = history
    ticker.get_shares_full.return_value = pd.Series([2_000_000], index=index)
    monkeypatch.setattr("autoscreener.collectors.yfinance_client.yf.Ticker", lambda _symbol: ticker)

    result = fetch_latest_price(
        "ZZSPLIT",
        RetryConfig(max_attempts=1, backoff_base_seconds=0.001, backoff_max_seconds=0.001),
        include_shares=False,
    )

    ticker.get_shares_full.assert_called_once()
    assert result is not None
    assert result["shares_outstanding"] == 2_000_000
    assert result["_shares_requested"] is True
