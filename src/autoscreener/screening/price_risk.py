"""Observed price-risk statistics used for display only (L-1)."""
from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PriceRisk:
    observation_days: int
    realized_vol_1y: float | None
    max_drawdown_1y: float | None
    max_drawdown_3y: float | None
    max_drawdown_days_3y: int | None
    recovery_days_3y: int | None
    currently_in_drawdown: float | None
    beta_1y: float | None
    downside_capture_1y: float | None
    benchmark_symbol: str | None = None


def _returns(series: Sequence[tuple[datetime.date, float]]) -> list[tuple[datetime.date, float]]:
    ordered = sorted((d, float(v)) for d, v in series if v > 0)
    return [(d, math.log(v / prior)) for (prior_d, prior), (d, v) in zip(ordered, ordered[1:])]


def _drawdown(series: Sequence[tuple[datetime.date, float]]) -> tuple[float | None, int | None, int | None, float | None]:
    ordered = sorted((d, float(v)) for d, v in series if v > 0)
    if not ordered:
        return None, None, None, None
    peak_value, peak_date = ordered[0][1], ordered[0][0]
    worst, worst_peak_date, trough_date = 0.0, peak_date, peak_date
    for date, value in ordered:
        if value > peak_value:
            peak_value, peak_date = value, date
        dd = value / peak_value - 1.0
        if dd < worst:
            worst, worst_peak_date, trough_date = dd, peak_date, date
    recovery_days = None
    if worst < 0:
        threshold = next(v for d, v in ordered if d == worst_peak_date)
        recovery = next((d for d, v in ordered if d > trough_date and v >= threshold), None)
        if recovery is not None:
            recovery_days = (recovery - trough_date).days
    current_dd = ordered[-1][1] / max(v for _, v in ordered) - 1.0
    return worst, (trough_date - worst_peak_date).days if worst < 0 else 0, recovery_days, current_dd


def compute_price_risk(
    closes: Sequence[tuple[datetime.date, float]],
    benchmark_closes: Sequence[tuple[datetime.date, float]] | None,
    *,
    min_observations: int = 60,
) -> PriceRisk | None:
    """Compute only observed values; insufficient values remain ``None``."""
    ordered = sorted((d, float(v)) for d, v in closes if v is not None and v > 0)
    if not ordered:
        return None
    one_year = ordered[-253:]
    three_year = ordered[-756:]
    observation_days = len(one_year)
    own_returns = _returns(one_year)
    vol = None
    if len(own_returns) >= min_observations - 1:
        mean = sum(v for _, v in own_returns) / len(own_returns)
        variance = sum((v - mean) ** 2 for _, v in own_returns) / (len(own_returns) - 1)
        vol = math.sqrt(variance * 252)
    dd1, _, _, _ = _drawdown(one_year) if len(one_year) >= min_observations else (None, None, None, None)
    dd3, dd_days, recovery, current_dd = _drawdown(three_year)
    if len(three_year) < min_observations:
        dd3 = dd_days = recovery = current_dd = None
    beta = capture = None
    if benchmark_closes:
        benchmark_returns = dict(_returns(benchmark_closes[-253:]))
        joined = [(r, benchmark_returns[d]) for d, r in own_returns if d in benchmark_returns]
        if len(joined) >= min_observations - 1:
            own, bench = zip(*joined)
            mean_b = sum(bench) / len(bench)
            var_b = sum((v - mean_b) ** 2 for v in bench) / (len(bench) - 1)
            if var_b > 0:
                cov = sum((x - sum(own) / len(own)) * (y - mean_b) for x, y in joined) / (len(joined) - 1)
                beta = cov / var_b
            downside = [(x, y) for x, y in joined if y < 0]
            if downside:
                denom = sum(y for _, y in downside)
                if denom != 0:
                    capture = sum(x for x, _ in downside) / denom
    return PriceRisk(observation_days, vol, dd1, dd3, dd_days, recovery, current_dd, beta, capture)
