"""Provider-neutral analyst consensus snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import Callable, Protocol


@dataclass(frozen=True)
class ConsensusSnapshot:
    observed_at: datetime.datetime
    source: str
    period_type: str
    period_end: datetime.date | None
    revenue_mean: float | None = None
    revenue_low: float | None = None
    revenue_high: float | None = None
    eps_mean: float | None = None
    ebitda_mean: float | None = None
    analyst_count: int | None = None
    target_price_mean: float | None = None
    raw_payload: dict | None = None
    source_url: str | None = None


class ConsensusProvider(Protocol):
    name: str
    def fetch(self, ticker: str, as_of: datetime.datetime) -> list[ConsensusSnapshot]: ...


def _number(value) -> float | None:
    try:
        if value is None or value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class YfinanceConsensusProvider:
    """Initial provider. The stored schema does not depend on yfinance."""
    name = "yfinance"

    def __init__(self, ticker_factory: Callable | None = None):
        if ticker_factory is None:
            import yfinance as yf
            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory

    def fetch(self, ticker: str, as_of: datetime.datetime) -> list[ConsensusSnapshot]:
        obj = self._ticker_factory(ticker)
        revenue = getattr(obj, "revenue_estimate", None)
        earnings = getattr(obj, "earnings_estimate", None)
        info = getattr(obj, "info", {}) or {}
        rows: list[ConsensusSnapshot] = []
        if revenue is None or getattr(revenue, "empty", True):
            return rows
        for label, row in revenue.iterrows():
            label_text = str(label)
            # revenue_estimate contains quarterly (0q/+1q) and annual
            # (0y/+1y) rows.  This collector stores annual consensus for the
            # reverse-valuation horizon; treating 0q as FY created the same
            # (source, period_end) key as 0y and aborted the whole batch.
            if not label_text.lower().endswith("y"):
                continue
            # Persist the provider label while using a deterministic
            # approximate calendar-year end.
            years = 2 if "+2" in label_text else 1 if "+1" in label_text else 0
            period_end = datetime.date(as_of.year + years, 12, 31)
            eps_row = earnings.loc[label] if earnings is not None and label in earnings.index else {}
            rows.append(ConsensusSnapshot(
                observed_at=as_of, source=self.name, period_type="FY", period_end=period_end,
                revenue_mean=_number(row.get("avg")), revenue_low=_number(row.get("low")),
                revenue_high=_number(row.get("high")), eps_mean=_number(getattr(eps_row, "get", lambda *_: None)("avg")),
                analyst_count=int(row.get("numberOfAnalysts")) if _number(row.get("numberOfAnalysts")) is not None else None,
                target_price_mean=_number(info.get("targetMeanPrice")),
                raw_payload={"provider_period": label_text},
            ))
        return rows
