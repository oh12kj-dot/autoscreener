from unittest.mock import Mock

import pytest
import requests
from yfinance.exceptions import YFDataException, YFRateLimitError, YFTickerMissingError

from autoscreener.collectors.errors import (
    ParseFailure,
    PermanentFailure,
    TransientFailure,
    classify_exception,
)


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
