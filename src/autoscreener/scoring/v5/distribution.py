"""Mathematically explicit Model v5 scenario-mixture distribution."""

from __future__ import annotations

import math
from statistics import NormalDist

from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.scenario import ReturnScenario

_NORMAL = NormalDist()


def base_distribution(result: MoicResult | None, *, target_moic: float) -> dict:
    """Backward-compatible Phase 1 seed serializer used by old persisted runs."""
    if result is None:
        return {"contract_version": "v5.phase1.base", "status": "unavailable",
                "distribution_family": None, "source_model_version": "v4", "p_target": None,
                "target_moic": target_moic, "expected_moic": None, "median_moic": None,
                "log_moic_mu": None, "log_moic_sigma": None, "survival_probability": None}
    return {"contract_version": "v5.phase1.base", "status": "base_only",
            "distribution_family": "v4_lognormal_seed", "source_model_version": "v4",
            "p_target": result.probability, "target_moic": target_moic,
            "expected_moic": result.expected_moic, "median_moic": result.median_moic,
            "log_moic_mu": result.log_moic_mu, "log_moic_sigma": result.log_moic_sigma,
            "survival_probability": result.survival_probability}


def _cdf(value: float, scenarios: tuple[ReturnScenario, ...], survival: float) -> float:
    if value <= 0:
        return 1.0 - survival
    conditional = sum(
        scenario.weight * _NORMAL.cdf(
            (math.log(value) - scenario.log_mu) / scenario.log_sigma
        )
        for scenario in scenarios
    )
    return min(1.0, max(0.0, (1.0 - survival) + survival * conditional))


def _quantile(q: float, scenarios: tuple[ReturnScenario, ...], survival: float) -> float:
    failure_mass = 1.0 - survival
    if q <= failure_mass or survival <= 0:
        return 0.0
    low = min(s.log_mu - 12.0 * s.log_sigma for s in scenarios)
    high = max(s.log_mu + 12.0 * s.log_sigma for s in scenarios)
    for _ in range(100):
        mid = (low + high) / 2.0
        if _cdf(math.exp(mid), scenarios, survival) < q:
            low = mid
        else:
            high = mid
    return math.exp((low + high) / 2.0)


def _expected_shortfall(
    alpha: float, scenarios: tuple[ReturnScenario, ...], survival: float
) -> float:
    if 1.0 - survival >= alpha:
        return 0.0
    cutoff = _quantile(alpha, scenarios, survival)
    truncated = 0.0
    for scenario in scenarios:
        z = (
            math.log(cutoff) - scenario.log_mu - scenario.log_sigma**2
        ) / scenario.log_sigma
        truncated += (
            scenario.weight
            * scenario.conditional_expected_moic
            * _NORMAL.cdf(z)
        )
    return survival * truncated / alpha


def _conditional_expected_moic_below(
    cutoff: float, scenarios: tuple[ReturnScenario, ...], survival: float
) -> float | None:
    """``E[MOIC | MOIC < cutoff]`` -- Phase 10 fix (docs/model_v5_phase10_*.md).

    ``expected_shortfall_10pct`` (``_expected_shortfall`` above) is defined
    at a *fixed probability level* (its own 10% quantile). When the failure
    atom alone already exceeds that level -- true for essentially every
    ticker in the real universe, since ``1.0 - survival`` commonly exceeds
    0.10 -- the 10%-quantile falls exactly on MOIC=0 and the whole measure
    collapses to a constant 0.0 for every ticker (measured: 100% of 1,164
    available rows on 2026-09-02). A constant carries no ranking
    information, which is why ``risk_adjusted`` was silently reproducing
    ``expected_return``'s exact rank order.

    This is a *fixed-cutoff* conditional expectation instead: "if this
    investment loses money (MOIC < 1.0), how much is lost on average,
    across both the failure atom (contributes exactly 0 to the numerator,
    same math as `_expected_shortfall`) and the surviving-but-low-return
    part of the continuous mixture below 1.0x". Because the *ratio* of
    failure-atom mass to sub-1.0x continuous mass differs by ticker
    (varying survival, sigma, conditional means), this does not collapse
    to a constant the way the alpha-quantile measure does -- see the
    Phase 10 evidence doc for the measured cross-sectional spread.

    Returns ``None`` (not 0.0) when essentially nobody loses money at this
    cutoff (``P(MOIC < cutoff)`` below a numerical floor) -- an honest
    "undefined", not a fabricated worst-case or best-case number.
    """
    mass_below = _cdf(cutoff, scenarios, survival)
    if mass_below < 1e-9:
        return None
    truncated = 0.0
    for scenario in scenarios:
        z = (
            math.log(cutoff) - scenario.log_mu - scenario.log_sigma**2
        ) / scenario.log_sigma
        truncated += (
            scenario.weight
            * scenario.conditional_expected_moic
            * _NORMAL.cdf(z)
        )
    return survival * truncated / mass_below


def unavailable_distribution(*, target_moic: float, confidence: float) -> dict:
    fields = {
        "p_moic_below_0_5": None, "p_moic_below_1_0": None,
        "p_moic_2x": None, "p_moic_3x": None, "p_moic_5x": None,
        "p_moic_10x": None, "p_target": None, "expected_moic": None,
        "median_moic": None, "expected_cagr": None, "median_cagr": None,
        "expected_shortfall_10pct": None, "p10_moic": None,
        "p25_moic": None, "p50_moic": None, "p75_moic": None,
        "p90_moic": None, "survival_probability": None,
        "acquisition_probability": None,
        # Phase 10 additions (docs/model_v5_phase10_*.md): additive-only,
        # never populated for an unavailable distribution, same as every
        # other field above.
        "expected_moic_given_loss": None,
        "reliability_sigma_multiplier": None,
        "reliability_left_tail_extra": None,
    }
    return {
        "contract_version": "v5.phase2", "status": "unavailable",
        "distribution_family": None,
        "source_model_version": "v4_structural_seed",
        "target_moic": target_moic, "model_confidence": confidence,
        "scenarios": [], **fields,
    }


def scenario_distribution(
    scenarios: tuple[ReturnScenario, ...],
    *,
    horizon_years: int,
    target_moic: float,
    confidence: float,
    sigma_multiplier: float = 1.0,
    left_tail_extra: float = 0.0,
) -> dict:
    """Return the full Phase 2 contract, including failure mass and ES10.

    ``sigma_multiplier``/``left_tail_extra`` (Phase 10 addition) are the
    *same* values already passed into ``build_scenarios`` by the caller --
    they do not change anything about how ``scenarios`` was built (this
    function never re-derives or second-guesses the scenario mixture it is
    handed). They are recorded here purely as diagnostic passthrough fields
    (``reliability_sigma_multiplier``/``reliability_left_tail_extra``) so
    ``objectives.py`` can read, from the persisted distribution alone, how
    much of this ticker's dispersion was widened by a reliability/quality
    signal -- needed to stop ``ten_bagger`` from mechanically rewarding
    that widening (see docs/model_v5_phase10_*.md). Purely additive: no
    existing field's value changes because of these two parameters.
    """
    if not scenarios:
        return unavailable_distribution(
            target_moic=target_moic, confidence=confidence
        )
    survival = scenarios[0].survival_probability
    if any(abs(s.survival_probability - survival) > 1e-12 for s in scenarios):
        raise ValueError("Phase 2 scenarios must share a survival probability")
    if abs(sum(s.weight for s in scenarios) - 1.0) > 1e-9:
        raise ValueError("scenario weights must sum to one")

    def exceed(threshold: float) -> float:
        return max(0.0, 1.0 - _cdf(threshold, scenarios, survival))

    expected_moic = survival * sum(
        s.weight * s.conditional_expected_moic for s in scenarios
    )
    quantiles = {
        q: _quantile(q, scenarios, survival)
        for q in (0.10, 0.25, 0.50, 0.75, 0.90)
    }
    median = quantiles[0.50]
    expected_cagr = expected_moic ** (1.0 / horizon_years) - 1.0
    median_cagr = median ** (1.0 / horizon_years) - 1.0 if median > 0 else -1.0
    return {
        "contract_version": "v5.phase2", "status": "available",
        "distribution_family": "failure_atom_plus_scenario_lognormal_mixture",
        "source_model_version": "v4_structural_seed",
        "target_moic": target_moic,
        "p_moic_below_0_5": _cdf(0.5, scenarios, survival),
        "p_moic_below_1_0": _cdf(1.0, scenarios, survival),
        "p_moic_2x": exceed(2.0), "p_moic_3x": exceed(3.0),
        "p_moic_5x": exceed(5.0), "p_moic_10x": exceed(10.0),
        "p_target": exceed(target_moic),
        "expected_moic": expected_moic, "median_moic": median,
        "expected_cagr": expected_cagr, "median_cagr": median_cagr,
        "expected_shortfall_10pct": _expected_shortfall(0.10, scenarios, survival),
        "p10_moic": quantiles[0.10], "p25_moic": quantiles[0.25],
        "p50_moic": median, "p75_moic": quantiles[0.75],
        "p90_moic": quantiles[0.90],
        "survival_probability": survival,
        "acquisition_probability": None,
        "model_confidence": confidence,
        "scenarios": [s.to_dict() for s in scenarios],
        "expected_moic_given_loss": _conditional_expected_moic_below(
            1.0, scenarios, survival
        ),
        "reliability_sigma_multiplier": sigma_multiplier,
        "reliability_left_tail_extra": left_tail_extra,
    }
