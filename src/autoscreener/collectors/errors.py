"""Failure taxonomy for data collection (要件定義書 18.1).

A single ``except Exception`` around network calls hides the difference between
"this ticker is gone forever" and "Yahoo throttled us for a minute" — the two
require opposite retry behavior. Every failure raised by the collector layer
must be one of the four types below so callers can dispatch on type, not on
string-matching an error message.
"""

from __future__ import annotations

import requests
import traceback
from yfinance.exceptions import (
    YFDataException,
    YFException,
    YFRateLimitError,
    YFTickerMissingError,
)


class CollectionError(Exception):
    """Base class for all classified collection failures."""


class PermanentFailure(CollectionError):
    """The ticker will not recover on retry (delisted, renamed, never existed).

    Callers should update ``tickers.delisted_at`` and stop retrying rather than
    queue this ticker for backoff.
    """


class TransientFailure(CollectionError):
    """A retryable failure: rate limiting, timeout, connection reset.

    Callers should retry with backoff within the same run; only after repeated
    transient failures across multiple days should a ticker be quarantined.
    """


class EmptyResponseError(CollectionError):
    """The request succeeded but returned no usable data (``info`` is `{}` or
    all key fields are ``None``). Distinct from a permanent failure: this can
    be a temporary Yahoo-side gap that resolves within days (see 13.1/13.5).
    """


class ParseFailure(CollectionError):
    """The response has an unexpected shape (missing keys, changed types).

    This is the highest-priority failure to surface: per 11章/14.14, it is the
    earliest signal that yfinance's upstream schema changed and the collector
    needs code changes, not just a retry.
    """


class YFinanceSessionFailure(TransientFailure):
    """The one observed yfinance cookie/session failure eligible for retry.

    This intentionally is not a blanket ``TypeError`` policy.  The caller must
    establish both the exact message and a yfinance stack frame before raising
    it; all other type errors remain parse failures.
    """


def is_known_yfinance_session_typeerror(exc: Exception) -> bool:
    """Return true only for the documented transient yfinance failure shape."""
    if not isinstance(exc, TypeError) or str(exc) != "argument of type 'NoneType' is not iterable":
        return False
    for frame in traceback.extract_tb(exc.__traceback__):
        normalized = "/" + frame.filename.replace("\\", "/").lstrip("/")
        if "/yfinance/" in normalized:
            return True
    return False


def classify_exception(exc: Exception) -> CollectionError:
    """Map a raw exception from the yfinance/requests call stack onto our taxonomy.

    Unknown exception types are treated as ParseFailure rather than silently
    retried — an unrecognized failure mode is exactly the kind of upstream
    change 18.1 wants surfaced immediately, not swallowed as "transient".
    """
    if isinstance(exc, YFRateLimitError):
        return TransientFailure(str(exc))

    if isinstance(exc, YFTickerMissingError):
        # Covers YFTickerMissingError and its subclasses (YFPricesMissingError,
        # YFTzMissingError) — yfinance raises these when Yahoo has no record
        # of the symbol at all, i.e. delisted or never listed.
        return PermanentFailure(str(exc))

    if isinstance(exc, YFDataException):
        return ParseFailure(str(exc))

    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return PermanentFailure(str(exc))
        # 401 "Invalid Crumb"(24.5で実データにて確認):Yahoo側のセッション/認証
        # トークンが累積リクエスト量で無効化される一時的な事象であり、
        # スキーマ変更を示すものではない。429/5xxと同様にリトライ対象とする。
        if status in (401, 429, 500, 502, 503, 504):
            return TransientFailure(str(exc))
        return ParseFailure(f"unexpected HTTP status {status}: {exc}")

    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return TransientFailure(str(exc))

    if isinstance(exc, YFException):
        # Any other yfinance-specific exception we haven't special-cased yet.
        return ParseFailure(str(exc))

    return ParseFailure(f"unclassified exception {type(exc).__name__}: {exc}")
