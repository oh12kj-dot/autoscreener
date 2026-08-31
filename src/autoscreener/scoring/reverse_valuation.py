"""Market-implied growth, kept outside the ranking model.

The solver deliberately reuses the core growth fade, margin and balance-sheet
assumptions, but never writes its result to ``scores.probability``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from autoscreener.config import ScoringConfig
from autoscreener.scoring.moic import (
    MoicInputs,
    growth_fade,
    growth_path,
    projected_net_debt,
    terminal_gross_margin,
)


@dataclass(frozen=True)
class ReverseValuationScenario:
    required_return: float
    implied_revenue_cagr: float | None
    implied_terminal_margin: float | None
    implied_terminal_multiple: float | None
    feasible: bool
    reason: str | None = None


def _terminal_equity(
    inputs: MoicInputs,
    config: ScoringConfig,
    initial_growth: float,
    terminal_margin: float,
    terminal_multiple: float,
) -> float:
    fade = growth_fade(inputs, config)
    rates = growth_path(initial_growth, fade, config)
    revenue = inputs.revenue_latest
    for rate in rates:
        revenue *= max(0.0, 1.0 + rate)
    dilution = inputs.dilution_cagr if inputs.dilution_cagr is not None else 0.0
    dilution = max(config.dilution.min_annual_rate, min(config.dilution.max_annual_rate, dilution))
    net_debt = projected_net_debt(inputs, rates, terminal_margin, dilution, config)
    return revenue * terminal_margin * terminal_multiple - net_debt


def solve_implied_growth(
    inputs: MoicInputs,
    config: ScoringConfig,
    required_return: float,
    *,
    terminal_margin: float | None = None,
    terminal_multiple: float | None = None,
) -> ReverseValuationScenario:
    """Solve the initial revenue CAGR required by today's market cap.

    A bounded bisection makes infeasible prices explicit instead of silently
    extrapolating an absurd growth rate. Negative growth is allowed down to
    -95%; the upper bound follows the core model's configured ceiling.
    """
    if inputs.market_cap <= 0 or inputs.revenue_latest <= 0:
        return ReverseValuationScenario(required_return, None, None, None, False, "invalid_inputs")
    if required_return <= -1:
        return ReverseValuationScenario(required_return, None, None, None, False, "invalid_required_return")

    margin = terminal_margin if terminal_margin is not None else terminal_gross_margin(inputs, config)
    current_ev = inputs.market_cap + inputs.net_debt
    current_multiple = current_ev / inputs.gross_profit_latest if inputs.gross_profit_latest > 0 else None
    multiple = terminal_multiple if terminal_multiple is not None else current_multiple
    cap = getattr(config.multiple, "absolute_cap", None)
    if multiple is not None and cap is not None:
        multiple = min(multiple, cap)
    if margin <= 0 or multiple is None or multiple <= 0:
        return ReverseValuationScenario(required_return, None, margin, multiple, False, "non_positive_terminal_value")

    required_equity = inputs.market_cap * (1.0 + required_return) ** config.horizon_years
    lower = -0.95
    upper = max(config.growth.max_initial_rate, 0.50)
    low_value = _terminal_equity(inputs, config, lower, margin, multiple)
    high_value = _terminal_equity(inputs, config, upper, margin, multiple)
    if required_equity < low_value:
        return ReverseValuationScenario(required_return, lower, margin, multiple, True, "below_solver_floor")
    if required_equity > high_value or not math.isfinite(high_value):
        return ReverseValuationScenario(required_return, None, margin, multiple, False, "growth_above_core_ceiling")

    for _ in range(100):
        mid = (lower + upper) / 2.0
        if _terminal_equity(inputs, config, mid, margin, multiple) < required_equity:
            lower = mid
        else:
            upper = mid
    implied = (lower + upper) / 2.0
    return ReverseValuationScenario(required_return, implied, margin, multiple, True)


def solve_scenarios(
    inputs: MoicInputs,
    config: ScoringConfig,
    required_returns: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30),
    *,
    horizon_years: int | None = None,
) -> list[ReverseValuationScenario]:
    effective = replace(config, horizon_years=horizon_years) if horizon_years is not None else config
    return [solve_implied_growth(inputs, effective, rate) for rate in required_returns]
