"""WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md) -- path risk estimated
from *realized price history*, not from the V4 lognormal seed.

Three prior work packages (WP-B2, WP-D; see
``docs/racr_shadow_run_diagnostic_2026-09-04.md`` §6) tried to give
``risk_adjusted_compounding`` (RACR) an independent risk dimension and each
one failed for the same reason: every quantity involved (``tail_loss_10``,
``failure_loss``/``survival_probability``, ``model_confidence``) is derived
from the *same* ``log_moic_mu``/``log_moic_sigma``/``survival_probability``
that also produces ``ce_cagr``, so all of them are collinear with it by
construction (measured: cond_tail_loss_10 vs ce_cagr rho=-0.855,
failure_prob vs ce_cagr rho=-0.696).

This module is the one place in ``scoring/v5`` that deliberately does *not*
touch ``MoicResult``, ``ReturnScenario``, ``log_moic_mu``/``log_moic_sigma``,
or ``survival_probability`` at all. Its only inputs are a ticker's own
realized ``price_snapshots`` rows (strictly ``trade_date <= as_of`` -- PIT)
and the model's target horizon. If a future edit makes this module read
anything derived from the V4 seed, the independence this whole work package
exists to create is gone and the result will collapse back into the same
failure the three documents above describe -- see the module-level warning
in ``docs/racr_wp_f1_path_risk_2026-09-04.md``.

## What this is: an honest description

This is **not** a full joint path simulation (correlated company/factor
residual paths per audit §7.4 item 6 -- that is later, larger work). It is
a **block-bootstrap historical-simulation estimator**:

1. Convert the ticker's own daily closes (+ dividends, added to the daily
   price return the same way ``backtest/runner.py``'s ``_realized_return``
   adds dividends to a holding-period return -- an accepted simplification,
   not full dividend-reinvestment share-count compounding) into
   non-overlapping ~weekly (5-trading-day) compounded total returns.
2. Resample that weekly return series with a **moving block bootstrap**
   (block length ``BLOCK_WEEKS``, sampled with replacement) to synthesize
   many candidate ``horizon_years``-long future weekly return paths. Each
   simulated path reuses only returns the company itself actually
   realized, in contiguous chunks long enough to preserve short-horizon
   volatility clustering/autocorrelation -- it does not assume a
   parametric process (no GBM, no Student-t fit, nothing borrowed from the
   V4 seed).
3. For each simulated path, compute the realized maximum drawdown and,
   if the path recovers from its worst drawdown before the horizon ends,
   the recovery time.
4. Aggregate across simulations into ``expected_max_drawdown``,
   ``P(MDD > 30/50/70%)``, ``DDExcess = E[max(MDD - 35%, 0)]``, and
   recovery-time quantiles.

This is a legitimate, standard technique (block-bootstrap historical
simulation is widely used for VaR/CVaR estimation precisely because it
lets a shorter observed sample stand in for a longer future window without
assuming a parametric distribution) but it has real limitations, stated
plainly rather than papered over:

- It assumes the future statistical behaviour of this company's own stock
  (volatility, autocorrelation, tail shape) resembles its own trading
  history. A structural regime change (IPO lockup expiry, new product
  cycle, capital structure change) is not modeled.
- Blocks are resampled independently across simulations *within* one
  ticker, but this module makes no attempt to correlate outcomes *across*
  tickers (no shared macro/factor shock) -- that cross-sectional
  correlation structure is exactly what audit §7.4 item 6's later "path
  risk" work item (joint factor/company residual paths) would add. Every
  ticker's simulation here is independent of every other ticker's.
- Recovery-time statistics are only reported once enough simulated
  drawdown episodes actually recover within the horizon
  (``MIN_RECOVERIES_FOR_REPORTING``); otherwise they stay ``None`` with an
  explicit reason rather than a median computed over a handful of
  survivors.
- Tickers with too little realized history (``MIN_DAILY_OBSERVATIONS``)
  return ``unavailable`` + a reason, never a fabricated number.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from random import Random
from typing import Sequence

TRADING_DAYS_PER_YEAR = 252.0
DAYS_PER_WEEK = 5  # trading days aggregated into one weekly bar before bootstrapping

# Minimum number of *daily* price observations (~2 years) before this
# ticker's realized volatility/tail behaviour is considered representative
# enough to bootstrap from. Below this, only a handful of distinct blocks
# would exist to resample, and the simulated cross-path dispersion would be
# an artifact of that small sample rather than a measurement of the
# company's actual behaviour.
MIN_DAILY_OBSERVATIONS = 504

# Same floor, expressed in aggregated weekly bars (used as the actual gate
# after weekly aggregation, since a handful of daily rows can be dropped by
# the return-computation step above -- e.g. a missing close).
MIN_WEEKLY_BARS = MIN_DAILY_OBSERVATIONS // DAYS_PER_WEEK

# ~1 month: long enough to preserve short-horizon volatility clustering and
# serial correlation in the resampled path, short enough that a single
# historical episode cannot make up more than a small fraction of any one
# simulated horizon.
BLOCK_WEEKS = 4

DEFAULT_SIMULATIONS = 300

DD_EXCESS_THRESHOLD = 0.35
MDD_THRESHOLDS: tuple[float, ...] = (0.30, 0.50, 0.70)

# A median/P90 recovery time computed from fewer simulated recoveries than
# this reads as more precise than it is -- reported as unavailable instead.
MIN_RECOVERIES_FOR_REPORTING = 20

INSUFFICIENT_HISTORY_REASON = "insufficient_price_history"
INSUFFICIENT_RECOVERIES_REASON = "insufficient_recoveries_within_horizon"


@dataclass(frozen=True)
class PriceObservation:
    """One PIT-visible price row. Deliberately just the three fields this
    module needs (not the full ``PriceSnapshot`` ORM row) so this module
    never depends on the DB layer and stays unit-testable with plain
    values, matching ``distribution.py``'s ``ReturnScenario`` convention.
    """

    trade_date: datetime.date
    close: float | None
    dividend: float | None = None


@dataclass(frozen=True)
class PathRiskResult:
    status: str  # "available" | "unavailable"
    unavailable_reason: str | None
    expected_max_drawdown: float | None
    p_mdd_above_30: float | None
    p_mdd_above_50: float | None
    p_mdd_above_70: float | None
    dd_excess: float | None
    recovery_time_median_days: float | None
    recovery_time_p90_days: float | None
    recovery_time_unavailable_reason: str | None
    observations_used: int
    weekly_bars_used: int
    simulations: int
    fraction_drawdowns_recovered: float | None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "expected_max_drawdown": self.expected_max_drawdown,
            "p_mdd_above_30": self.p_mdd_above_30,
            "p_mdd_above_50": self.p_mdd_above_50,
            "p_mdd_above_70": self.p_mdd_above_70,
            "dd_excess": self.dd_excess,
            "recovery_time_median_days": self.recovery_time_median_days,
            "recovery_time_p90_days": self.recovery_time_p90_days,
            "recovery_time_unavailable_reason": self.recovery_time_unavailable_reason,
            "observations_used": self.observations_used,
            "weekly_bars_used": self.weekly_bars_used,
            "simulations": self.simulations,
            "fraction_drawdowns_recovered": self.fraction_drawdowns_recovered,
        }


def _unavailable(reason: str, *, observations_used: int = 0) -> PathRiskResult:
    return PathRiskResult(
        status="unavailable",
        unavailable_reason=reason,
        expected_max_drawdown=None,
        p_mdd_above_30=None,
        p_mdd_above_50=None,
        p_mdd_above_70=None,
        dd_excess=None,
        recovery_time_median_days=None,
        recovery_time_p90_days=None,
        recovery_time_unavailable_reason=reason,
        observations_used=observations_used,
        weekly_bars_used=0,
        simulations=0,
        fraction_drawdowns_recovered=None,
    )


def _weekly_total_returns(observations: list[PriceObservation]) -> list[float]:
    """Non-overlapping ~weekly compounded total returns from daily closes.

    Daily total return: ``(close_t + dividend_t) / close_{t-1} - 1``. This
    adds the dividend to the price return rather than reinvesting it via a
    share count -- the same convention ``backtest/runner.py``'s
    ``_realized_return`` uses for holding-period returns. Acceptable at
    daily granularity since a single day's dividend is small relative to
    price; not a claim of exact total-return replication.
    """
    daily_returns: list[float] = []
    for prev, cur in zip(observations, observations[1:]):
        if prev.close is None or cur.close is None or prev.close <= 0:
            continue
        dividend = cur.dividend or 0.0
        r = (cur.close + dividend) / prev.close - 1.0
        if math.isfinite(r):
            daily_returns.append(r)
    weekly: list[float] = []
    for start in range(0, len(daily_returns) - DAYS_PER_WEEK + 1, DAYS_PER_WEEK):
        chunk = daily_returns[start:start + DAYS_PER_WEEK]
        total = 1.0
        for r in chunk:
            total *= 1.0 + r
        weekly.append(total - 1.0)
    return weekly


def _sample_bootstrap_path(
    weekly_returns: Sequence[float], *, horizon_weeks: int, block_weeks: int, rng: Random,
) -> list[float]:
    """Moving block bootstrap: repeatedly pick a random contiguous block of
    ``block_weeks`` real historical weekly returns (with replacement across
    draws) and concatenate until the synthesized path reaches
    ``horizon_weeks``, then truncate to exactly that length."""
    n = len(weekly_returns)
    block = min(block_weeks, n)
    path: list[float] = []
    while len(path) < horizon_weeks:
        start = rng.randrange(0, n - block + 1) if n > block else 0
        path.extend(weekly_returns[start:start + block])
    return path[:horizon_weeks]


def _max_drawdown_and_recovery(period_returns: Sequence[float]) -> tuple[float, int | None]:
    """``(MDD, weeks_to_recover)`` for one simulated wealth path.

    ``weeks_to_recover`` is the number of weeks from the drawdown's trough
    until wealth first returns to the level of the peak that preceded that
    trough, or ``None`` if the path never recovers within its own horizon
    (right-censored, not "recovers eventually").
    """
    series = [1.0]
    wealth = 1.0
    for r in period_returns:
        wealth *= 1.0 + r
        series.append(wealth)
    peak = series[0]
    peak_index = 0
    max_dd = 0.0
    trough_index = 0
    dd_peak_index = 0
    for i, w in enumerate(series):
        if w > peak:
            peak = w
            peak_index = i
        dd = 1.0 - w / peak
        if dd > max_dd:
            max_dd = dd
            trough_index = i
            dd_peak_index = peak_index
    if max_dd <= 0.0:
        return 0.0, 0
    target = series[dd_peak_index]
    for j in range(trough_index, len(series)):
        if series[j] >= target:
            return max_dd, j - trough_index
    return max_dd, None


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def estimate_path_risk(
    observations: Sequence[PriceObservation],
    *,
    as_of: datetime.date,
    horizon_years: float,
    simulations: int = DEFAULT_SIMULATIONS,
    block_weeks: int = BLOCK_WEEKS,
    seed: int | None = None,
) -> PathRiskResult:
    """Block-bootstrap historical-simulation estimate of path risk over
    ``horizon_years``, from ``observations`` alone.

    PIT: any observation with ``trade_date > as_of`` is dropped before any
    other computation -- this is the *only* leakage guard this function
    needs, since everything downstream (returns, bootstrap, drawdown) is a
    pure function of the filtered list.

    ``seed`` should be a stable, per-ticker value (e.g. derived from
    ``ticker_id`` and ``as_of``) so a given run is reproducible; leaving it
    ``None`` uses a fresh, unseeded ``Random()`` (nondeterministic, fine
    for ad hoc exploration but not for a persisted run).
    """
    pit = sorted(
        (obs for obs in observations if obs.trade_date <= as_of),
        key=lambda obs: obs.trade_date,
    )
    if len(pit) < MIN_DAILY_OBSERVATIONS + 1:
        return _unavailable(INSUFFICIENT_HISTORY_REASON, observations_used=len(pit))

    weekly_returns = _weekly_total_returns(pit)
    if len(weekly_returns) < MIN_WEEKLY_BARS:
        return _unavailable(INSUFFICIENT_HISTORY_REASON, observations_used=len(pit))

    horizon_weeks = max(1, round((TRADING_DAYS_PER_YEAR / DAYS_PER_WEEK) * horizon_years))
    rng = Random(seed)

    mdds: list[float] = []
    recoveries: list[int] = []
    n_drawdowns = 0
    for _ in range(simulations):
        path = _sample_bootstrap_path(
            weekly_returns, horizon_weeks=horizon_weeks, block_weeks=block_weeks, rng=rng,
        )
        mdd, recovery_weeks = _max_drawdown_and_recovery(path)
        mdds.append(mdd)
        if mdd > 0.0:
            n_drawdowns += 1
            if recovery_weeks is not None:
                recoveries.append(recovery_weeks)

    expected_max_drawdown = sum(mdds) / len(mdds)

    def _p_above(threshold: float) -> float:
        return sum(1 for m in mdds if m > threshold) / len(mdds)

    dd_excess = sum(max(0.0, m - DD_EXCESS_THRESHOLD) for m in mdds) / len(mdds)

    if len(recoveries) >= MIN_RECOVERIES_FOR_REPORTING:
        ordered = sorted(recoveries)
        recovery_time_median_days = median_weeks_to_days(ordered, 0.50)
        recovery_time_p90_days = median_weeks_to_days(ordered, 0.90)
        recovery_reason = None
    else:
        recovery_time_median_days = None
        recovery_time_p90_days = None
        recovery_reason = INSUFFICIENT_RECOVERIES_REASON

    fraction_recovered = (len(recoveries) / n_drawdowns) if n_drawdowns else None

    return PathRiskResult(
        status="available",
        unavailable_reason=None,
        expected_max_drawdown=expected_max_drawdown,
        p_mdd_above_30=_p_above(MDD_THRESHOLDS[0]),
        p_mdd_above_50=_p_above(MDD_THRESHOLDS[1]),
        p_mdd_above_70=_p_above(MDD_THRESHOLDS[2]),
        dd_excess=dd_excess,
        recovery_time_median_days=recovery_time_median_days,
        recovery_time_p90_days=recovery_time_p90_days,
        recovery_time_unavailable_reason=recovery_reason,
        observations_used=len(pit),
        weekly_bars_used=len(weekly_returns),
        simulations=simulations,
        fraction_drawdowns_recovered=fraction_recovered,
    )


def median_weeks_to_days(sorted_weeks: Sequence[int], p: float) -> float:
    """Weeks -> calendar days (``* 7``), at percentile ``p`` of a
    pre-sorted sample."""
    return _percentile([float(w) for w in sorted_weeks], p) * 7.0


def stable_seed(ticker_id: int, as_of: datetime.date) -> int:
    """Deterministic per-(ticker, as_of) seed so a persisted run's path-risk
    numbers are reproducible without storing RNG state. Not cryptographic;
    just needs to differ across tickers/dates."""
    return (int(ticker_id) * 2_654_435_761 + as_of.toordinal()) & 0x7FFFFFFF
