"""Scenario construction for the Phase 2 return-distribution contract."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from autoscreener.config import ModelV5Config
from autoscreener.scoring.moic import MoicResult


@dataclass(frozen=True)
class ReturnScenario:
    name: str
    weight: float
    log_mu: float
    log_sigma: float
    conditional_expected_moic: float
    survival_probability: float

    def to_dict(self) -> dict:
        return asdict(self)


def build_scenarios(
    result: MoicResult,
    *,
    confidence: float,
    config: ModelV5Config,
    conditional_mean_multiplier: float = 1.0,
    sigma_multiplier: float = 1.0,
    left_tail_extra: float = 0.0,
    survival_multiplier: float = 1.0,
) -> tuple[ReturnScenario, ...]:
    """Expand one structural seed into a mean-preserving scenario mixture.

    Confidence widens dispersion, not the mean. Scenario location multipliers
    are normalised so their weighted conditional expectation exactly equals the
    seed lognormal expectation.

    ``sigma_multiplier``/``left_tail_extra`` are the Phase 4 accounting-quality
    hooks (docs/model_v5_phase4_handoff_2026-09-03.md 4.5): they widen sigma
    (uniformly, and the downside scenario further) but never enter
    ``conditional_mean`` below, so the conditional expectation always stays
    exactly ``seed_conditional_mean * raw_multipliers[name] / normaliser``
    regardless of how much sigma widens -- accounting quality can only ever
    move probability mass, never the point estimate.

    ``survival_multiplier`` is the Phase 5 debt-maturity/liquidity/capital-
    allocation hook (docs/model_v5_phase5_capital_allocation_2026-09-03.md):
    survival was held fixed at the v4 seed through Phase 2-4 ("Survival is
    held constant until Phase 5/6" -- no longer true from Phase 5 on). It can
    only ever *shrink* survival_probability (``<= 1.0``), never grant a bonus
    above the v4-seeded baseline, enforced here as well as in
    ``balance_sheet.py`` (defense in depth, matching the sigma/left-tail
    guards above). All three multiplier/extra defaults (1.0/0.0/1.0)
    reproduce Phase 2/3/4 output exactly -- the regression guard asserted in
    every phase's test suite.
    """
    confidence = max(0.0, min(1.0, confidence))
    if sigma_multiplier < 1.0:
        raise ValueError("sigma_multiplier must be >= 1.0")
    if left_tail_extra < 0.0:
        raise ValueError("left_tail_extra must be >= 0.0")
    if not 0.0 < survival_multiplier <= 1.0:
        raise ValueError("survival_multiplier must be in (0, 1]")
    survival_probability = max(0.0, min(1.0, result.survival_probability * survival_multiplier))
    uncertainty = config.uncertainty
    widened_sigma = result.log_moic_sigma * (
        1.0 + uncertainty.confidence_sigma_multiplier * (1.0 - confidence)
    ) * sigma_multiplier
    shift = uncertainty.scenario_log_shift_sigma * result.log_moic_sigma
    weights = {
        "downside": config.scenario_weights.downside,
        "base": config.scenario_weights.base,
        "upside": config.scenario_weights.upside,
    }
    raw_multipliers = {"downside": math.exp(-shift), "base": 1.0, "upside": math.exp(shift)}
    normaliser = sum(weights[name] * raw_multipliers[name] for name in weights)
    if conditional_mean_multiplier <= 0:
        raise ValueError("conditional_mean_multiplier must be positive")
    seed_conditional_mean = (
        math.exp(result.log_moic_mu + result.log_moic_sigma**2 / 2.0)
        * conditional_mean_multiplier
    )

    scenarios: list[ReturnScenario] = []
    for name in ("downside", "base", "upside"):
        tail_multiplier = (
            uncertainty.left_tail_multiplier + left_tail_extra if name == "downside" else 1.0
        )
        scenario_sigma = widened_sigma * tail_multiplier
        conditional_mean = seed_conditional_mean * raw_multipliers[name] / normaliser
        log_mu = math.log(conditional_mean) - scenario_sigma**2 / 2.0
        scenarios.append(ReturnScenario(
            name=name, weight=weights[name], log_mu=log_mu, log_sigma=scenario_sigma,
            conditional_expected_moic=conditional_mean,
            survival_probability=survival_probability,
        ))
    return tuple(scenarios)
