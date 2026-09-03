"""Phase 10 (docs/model_v5_phase10_*.md) regression tests.

Two independent methodology defects, found by real-data ablation/severity
analysis on the 2026-09-02 universe (not by unit tests -- the existing
Phase 2-7 suite passed throughout, which is exactly why these needed a
dedicated real-data audit):

1. ``risk_adjusted`` was mathematically identical in rank order to
   ``expected_return`` because ``expected_shortfall_10pct`` (a fixed
   *probability-level* measure) collapsed to a constant 0.0 whenever the
   failure atom alone exceeded 10% -- true for 100% of the real universe.
2. ``ten_bagger`` mechanically rewarded a reliability/quality-driven
   widening of the distribution (e.g. accounting_quality severity), because
   a mean-preserving variance increase raises any far-right-tail exceedance
   probability -- a structural property of the lognormal-mixture family,
   confirmed in 712/712 real ablations before this fix.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from autoscreener.config import ObjectiveDefinition, ObjectivesConfig, load_model_v5_config
from autoscreener.scoring.v5.distribution import scenario_distribution
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.scenario import build_scenarios


def _seed_result(survival: float = 0.85):
    return SimpleNamespace(
        log_moic_mu=math.log(2.0) - 0.5 * 0.7**2,
        log_moic_sigma=0.7,
        survival_probability=survival,
    )


def _distribution(
    *, survival: float = 0.85, sigma_multiplier: float = 1.0, left_tail_extra: float = 0.0, confidence: float = 0.5,
) -> dict:
    config = load_model_v5_config()
    scenarios = build_scenarios(
        _seed_result(survival), confidence=confidence, config=config,
        sigma_multiplier=sigma_multiplier, left_tail_extra=left_tail_extra,
    )
    return scenario_distribution(
        scenarios, horizon_years=7, target_moic=10.0, confidence=confidence,
        sigma_multiplier=sigma_multiplier, left_tail_extra=left_tail_extra,
    )


def _objectives_config(*, sigma_lambda: float | None = 20.0, left_tail_lambda: float | None = 20.0) -> ObjectivesConfig:
    return ObjectivesConfig(
        default_objective="ten_bagger",
        objectives={
            "ten_bagger": ObjectiveDefinition(
                description="test", reliability_sigma_lambda=sigma_lambda,
                reliability_left_tail_lambda=left_tail_lambda,
            ),
            "expected_return": ObjectiveDefinition(description="test"),
            "risk_adjusted": ObjectiveDefinition(description="test", downside_lambda=0.50),
        },
    )


# -- Distribution contract additions -----------------------------------

def test_reliability_fields_are_diagnostic_passthrough_not_reinterpreted():
    """scenario_distribution must record exactly the sigma_multiplier/
    left_tail_extra it was handed -- it must never re-derive or clamp them
    differently from what build_scenarios actually used."""
    dist = _distribution(sigma_multiplier=1.35, left_tail_extra=0.20)
    assert dist["reliability_sigma_multiplier"] == pytest.approx(1.35)
    assert dist["reliability_left_tail_extra"] == pytest.approx(0.20)


def test_reliability_fields_default_to_the_no_widening_baseline():
    dist = _distribution()
    assert dist["reliability_sigma_multiplier"] == pytest.approx(1.0)
    assert dist["reliability_left_tail_extra"] == pytest.approx(0.0)


def test_expected_shortfall_10pct_kept_unchanged_for_backward_compatibility():
    """The old field/formula is untouched -- anything reading it directly
    keeps working exactly as before; only the risk_adjusted objective's
    *inputs* changed, not the distribution contract's existing fields."""
    dist = _distribution(survival=0.85)
    assert dist["expected_shortfall_10pct"] == 0.0


def test_expected_moic_given_loss_is_not_a_universal_constant():
    """Unlike expected_shortfall_10pct (identically 0.0 whenever the
    failure atom alone exceeds 10% -- true for every ticker in the real
    universe), the fixed-cutoff E[MOIC | MOIC < 1.0] must vary with the
    actual shape of the mixture below 1.0x, not just with whether the
    failure atom crosses one fixed probability threshold."""
    low_survival = _distribution(survival=0.60)
    high_survival = _distribution(survival=0.85)
    assert low_survival["expected_moic_given_loss"] is not None
    assert high_survival["expected_moic_given_loss"] is not None
    assert low_survival["expected_moic_given_loss"] != pytest.approx(
        high_survival["expected_moic_given_loss"]
    )


def test_expected_moic_given_loss_is_none_when_essentially_nobody_loses_money():
    # An extremely high survival + a mean far above 1.0x drives
    # P(MOIC < 1.0) to (numerically) zero; the statistic must report
    # "undefined" (None), never a fabricated 0.0 or 1.0.
    dist = _distribution(survival=0.999999999, confidence=1.0)
    if dist["p_moic_below_1_0"] < 1e-9:
        assert dist["expected_moic_given_loss"] is None


# -- Objective 1: risk_adjusted must stop reproducing expected_return -----

def test_risk_adjusted_is_not_identical_rank_order_to_expected_return():
    """Direct regression for the Phase 7 audit finding: two distributions
    with the same expected_cagr but a different downside shape (different
    left_tail_extra -> different expected_moic_given_loss) must not
    collapse to the same risk_adjusted-vs-expected_return relationship
    the old ES10-based formula produced (both were the same 0.0 for every
    survival <= 0.90, so risk_adjusted == expected_return - constant,
    Spearman exactly 1.0 against expected_return universe-wide)."""
    objectives_config = _objectives_config()
    mild = _distribution(survival=0.85, left_tail_extra=0.0)
    severe = _distribution(survival=0.85, left_tail_extra=0.35)

    mild_results = evaluate_objectives(mild, objectives_config, horizon_years=7)
    severe_results = evaluate_objectives(severe, objectives_config, horizon_years=7)

    # Old (broken) formula: both would be `expected_cagr - lambda * 1.0`,
    # so the ONLY thing separating the two risk_adjusted values would be
    # whatever expected_cagr difference the left_tail_extra widening
    # happened to also cause -- new formula must separate them via a
    # genuinely different expected_moic_given_loss instead.
    assert mild["expected_shortfall_10pct"] == 0.0
    assert severe["expected_shortfall_10pct"] == 0.0
    assert mild["expected_moic_given_loss"] != pytest.approx(severe["expected_moic_given_loss"])
    # The more severe left-tail widening must show a worse (lower)
    # expected_moic_given_loss -- confirms the new measure moves in the
    # economically sensible direction, not just "some different number".
    assert severe["expected_moic_given_loss"] < mild["expected_moic_given_loss"]
    assert severe_results["risk_adjusted"].score_value < mild_results["risk_adjusted"].score_value


def test_risk_adjusted_no_longer_a_constant_shift_of_expected_return():
    """A direct algebraic check: under the OLD formula, risk_adjusted -
    expected_return was always exactly -downside_lambda (a pure constant,
    since ES10_cagr was always -1.0). Under the fix it must not be."""
    objectives_config = _objectives_config()
    a = _distribution(survival=0.85, left_tail_extra=0.0)
    b = _distribution(survival=0.60, left_tail_extra=0.30)
    a_results = evaluate_objectives(a, objectives_config, horizon_years=7)
    b_results = evaluate_objectives(b, objectives_config, horizon_years=7)
    shift_a = a_results["risk_adjusted"].score_value - a_results["expected_return"].score_value
    shift_b = b_results["risk_adjusted"].score_value - b_results["expected_return"].score_value
    assert shift_a != pytest.approx(shift_b)


# -- Objective 2: ten_bagger must stop rewarding reliability widening -----

def test_ten_bagger_no_longer_rewards_reliability_driven_widening():
    """The core Phase 10 acceptance criterion (coordinator's constructed-
    example requirement): holding the conditional mean fixed (scenario.py's
    mean-preservation guarantee, untouched by this fix), a ticker whose
    ONLY difference is a worse reliability-driven widening (higher
    sigma_multiplier and left_tail_extra, e.g. from worse accounting_quality
    severity) must not score higher on the default objective than an
    otherwise-identical ticker without that widening.

    Real-data confirmation (Phase 10 evidence doc): before this fix, this
    exact comparison held in the WRONG direction for 712/712 real
    accounting_quality-affected tickers.
    """
    objectives_config = _objectives_config()
    clean = _distribution(survival=0.85, sigma_multiplier=1.0, left_tail_extra=0.0)
    widened = _distribution(survival=0.85, sigma_multiplier=1.5, left_tail_extra=0.35)

    # Mean preservation (Issue section 6.3) must hold regardless -- this
    # fix must never touch the distribution's own mean.
    assert widened["expected_moic"] == pytest.approx(clean["expected_moic"], rel=1e-9)
    assert widened["expected_cagr"] == pytest.approx(clean["expected_cagr"], rel=1e-9)

    # The raw (undiscounted) exceedance probability moves the "wrong" way
    # mechanically -- this is the bug being guarded against, not something
    # this fix removes from the distribution itself.
    assert widened["p_target"] > clean["p_target"]

    clean_results = evaluate_objectives(clean, objectives_config, horizon_years=7)
    widened_results = evaluate_objectives(widened, objectives_config, horizon_years=7)
    assert widened_results["ten_bagger"].score_value <= clean_results["ten_bagger"].score_value


def test_ten_bagger_discount_is_zero_when_lambdas_are_unset():
    """Backward compatibility: a config that doesn't set the new Phase 10
    fields (e.g. every pre-Phase-10 test fixture, and any objective other
    than ten_bagger) must reproduce the exact Phase 2 formula."""
    objectives_config = _objectives_config(sigma_lambda=None, left_tail_lambda=None)
    widened = _distribution(survival=0.85, sigma_multiplier=1.5, left_tail_extra=0.35)
    results = evaluate_objectives(widened, objectives_config, horizon_years=7)
    assert results["ten_bagger"].score_value == pytest.approx(widened["p_target"])


def test_ten_bagger_discount_is_zero_when_distribution_has_no_widening():
    """The discount must be exactly 0 (not merely small) when
    sigma_multiplier=1.0 and left_tail_extra=0.0 -- every pre-Phase-10
    persisted distribution and every ticker with no accounting_quality/
    tail-risk signal applied must be scored identically to before."""
    objectives_config = _objectives_config()
    clean = _distribution(survival=0.85, sigma_multiplier=1.0, left_tail_extra=0.0)
    results = evaluate_objectives(clean, objectives_config, horizon_years=7)
    assert results["ten_bagger"].score_value == pytest.approx(clean["p_target"])


def test_ten_bagger_explanation_reports_the_raw_value_and_the_discount():
    """UI/audit-facing: the explanation dict must expose enough for a
    reader to reconstruct the discount, not just the final number."""
    objectives_config = _objectives_config()
    widened = _distribution(survival=0.85, sigma_multiplier=1.5, left_tail_extra=0.35)
    results = evaluate_objectives(widened, objectives_config, horizon_years=7)
    explanation = results["ten_bagger"].explanation
    assert explanation["raw_p_target"] == pytest.approx(widened["p_target"])
    assert explanation["reliability_widening"] > 0.0
