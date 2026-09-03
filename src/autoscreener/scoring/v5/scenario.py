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
) -> tuple[ReturnScenario, ...]:
    """Expand one structural seed into a mean-preserving scenario mixture.

    Confidence widens dispersion, not the mean. Scenario location multipliers
    are normalised so their weighted conditional expectation exactly equals the
    seed lognormal expectation. Survival is held constant until Phase 5/6.
    """
    confidence = max(0.0, min(1.0, confidence))
    uncertainty = config.uncertainty
    widened_sigma = result.log_moic_sigma * (
        1.0 + uncertainty.confidence_sigma_multiplier * (1.0 - confidence)
    )
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
        scenario_sigma = widened_sigma * (uncertainty.left_tail_multiplier if name == "downside" else 1.0)
        conditional_mean = seed_conditional_mean * raw_multipliers[name] / normaliser
        log_mu = math.log(conditional_mean) - scenario_sigma**2 / 2.0
        scenarios.append(ReturnScenario(
            name=name, weight=weights[name], log_mu=log_mu, log_sigma=scenario_sigma,
            conditional_expected_moic=conditional_mean,
            survival_probability=result.survival_probability,
        ))
    return tuple(scenarios)
