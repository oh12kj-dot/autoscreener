"""FRED(セントルイス連銀)APIの薄いラッパー(30.8)。

`edgar_client.py` と同じ構造にしてある。SECほど厳格なレート制御は要らない
(FREDは通常のAPIキー方式で、公式にレート制限の数値を公表していないが、
実務上は緩い)ため、`RateLimiter` は流用しつつ既定レートを高めに取る。

**キー不要のCSVエンドポイント(fredgraph.csv)を採らない**(30.8.1)。あれは
グラフ描画用であり公式APIではなく規約上の位置づけが曖昧。キーは無料で
即発行できるため、正規のAPIキー方式のみをサポートする。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from autoscreener.collectors.edgar_client import RateLimiter
from autoscreener.collectors.errors import (
    EmptyResponseError,
    ParseFailure,
    PermanentFailure,
    TransientFailure,
)

FRED_SERIES_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# "." はFREDが欠測値を表す記法(週末・祝日等)。
_MISSING_VALUE_MARKERS = frozenset({".", ""})


@dataclass(frozen=True)
class SeriesObservation:
    observation_date: datetime.date
    value: float | None


class FredClient:
    def __init__(self, api_key: str, *, requests_per_second: float = 4.0, timeout_seconds: float = 15.0) -> None:
        if not api_key:
            raise ValueError(
                "FRED_API_KEY が未設定です。.env に FRED_API_KEY を設定してください"
                "(https://fred.stlouisfed.org/docs/api/api_key.html で無料発行できます)。"
            )
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._rate_limiter = RateLimiter(requests_per_second)
        self._session = requests.Session()

    def fetch_series(
        self, series_id: str, *, start_date: datetime.date | None = None
    ) -> list[SeriesObservation]:
        """1系列ぶんの観測値を取得する。"""
        self._rate_limiter.acquire()
        params = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        if start_date is not None:
            params["observation_start"] = start_date.isoformat()

        @retry(
            retry=retry_if_exception_type(TransientFailure),
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=1.0, max=15.0),
            reraise=True,
        )
        def _call() -> requests.Response:
            try:
                response = self._session.get(FRED_SERIES_OBSERVATIONS_URL, params=params, timeout=self._timeout)
                response.raise_for_status()
                return response
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (400, 404):
                    raise EmptyResponseError(f"FRED series {series_id}: {exc}") from exc
                if status in (401, 403):
                    raise PermanentFailure(f"FRED API key rejected: {exc}") from exc
                if status in (429, 500, 502, 503, 504):
                    raise TransientFailure(str(exc)) from exc
                raise ParseFailure(f"unexpected HTTP status {status}: {exc}") from exc
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                raise TransientFailure(str(exc)) from exc

        response = _call()
        try:
            data = response.json()
        except ValueError as exc:
            raise ParseFailure(f"FRED response is not valid JSON: {exc}") from exc

        observations = data.get("observations")
        if observations is None:
            raise ParseFailure(f"FRED response missing 'observations': {data}")

        result: list[SeriesObservation] = []
        for obs in observations:
            raw_value = obs.get("value")
            value = None if raw_value in _MISSING_VALUE_MARKERS else float(raw_value)
            result.append(
                SeriesObservation(observation_date=datetime.date.fromisoformat(obs["date"]), value=value)
            )
        return result
