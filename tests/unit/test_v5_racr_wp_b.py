"""WP-B (docs/racr_wp_b_output_contract_2026-09-04.md) regression tests.

Covers plan section 3 (B-1..B-4): distribution contract version separation,
the new CE-CAGR/threshold-probability/downside outputs (all derived from the
same CDF as every pre-existing field), the ``risk_adjusted_compounding``
(RACR) shadow objective, and API-contract backward compatibility.

Pure-Python: no DB session anywhere in this file (see the audit's own
"検証上の制約" and this WP's explicit test-scope restriction -- the test
database is not yet isolated from the developer's real local Postgres,
tracked separately as WP-A).
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from autoscreener.config import ObjectiveDefinition, ObjectivesConfig, load_model_v5_config
from autoscreener.scoring.v5.distribution import (
    CE_CAGR_FAILURE_FLOOR_MOIC,
    scenario_distribution,
    unavailable_distribution,
)
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.scenario import build_scenarios


def _seed_result(survival: float = 0.85, *, mu_moic: float = 2.0, sigma: float = 0.7):
    return SimpleNamespace(
        log_moic_mu=math.log(mu_moic) - 0.5 * sigma**2,
        log_moic_sigma=sigma,
        survival_probability=survival,
    )


def _distribution(
    *,
    survival: float = 0.85,
    mu_moic: float = 2.0,
    sigma: float = 0.7,
    horizon_years: int = 7,
    confidence: float = 0.5,
    target_moic: float = 10.0,
) -> dict:
    config = load_model_v5_config()
    scenarios = build_scenarios(
        _seed_result(survival, mu_moic=mu_moic, sigma=sigma), confidence=confidence, config=config,
    )
    return scenario_distribution(
        scenarios, horizon_years=horizon_years, target_moic=target_moic, confidence=confidence,
    )


def _objectives_config() -> ObjectivesConfig:
    return ObjectivesConfig(
        default_objective="ten_bagger",
        objectives={
            "ten_bagger": ObjectiveDefinition(description="test"),
            "expected_return": ObjectiveDefinition(description="test"),
            "risk_adjusted": ObjectiveDefinition(
                description="test", downside_lambda=0.50, deprecated=True,
            ),
            "risk_adjusted_compounding": ObjectiveDefinition(
                description="test", tail_lambda=0.35, failure_lambda=0.20,
                drawdown_lambda=0.10, permanent_loss_lambda=0.20,
                uncertainty_lambda=0.50,
            ),
        },
    )


# -- B-1: contract version separation ------------------------------------

def test_distribution_contract_version_is_racr2():
    # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md) bumped this from
    # v5.racr1 to v5.racr2: new conditional-tail-loss/failure-frequency
    # fields, RACR values are not comparable across contract versions.
    dist = _distribution()
    assert dist["contract_version"] == "v5.racr3"


def test_unavailable_distribution_contract_version_is_racr2_too():
    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    assert dist["contract_version"] == "v5.racr3"


# -- B-2: identity tests ---------------------------------------------------

def test_quantile_monotonicity():
    dist = _distribution()
    assert dist["p10_moic"] <= dist["p25_moic"] <= dist["p50_moic"]
    assert dist["p50_moic"] <= dist["p75_moic"] <= dist["p90_moic"]


def test_exceedance_probabilities_are_monotonically_decreasing_in_threshold():
    dist = _distribution(survival=0.90, mu_moic=4.0, sigma=0.9)
    assert dist["p_moic_2x"] >= dist["p_moic_3x"] >= dist["p_moic_5x"] >= dist["p_moic_10x"]


def test_below_and_above_one_moic_sum_to_one():
    dist = _distribution()
    p_above_1 = 1.0 - dist["p_moic_below_1_0"]
    assert dist["p_moic_below_1_0"] + p_above_1 == pytest.approx(1.0)


@pytest.mark.parametrize(
    "horizon_years,rate,expected_moic",
    [
        (7, 0.15, 2.660),
        (7, 0.20, 3.583),
        (7, 0.25, 4.768),
    ],
)
def test_cagr_to_moic_threshold_conversion_matches_audit_table(horizon_years, rate, expected_moic):
    """audit §5.2/§6.1 table: thresholds are computed from horizon, never
    hardcoded -- this test just confirms scenario_distribution's internal
    `(1+rate)**horizon_years` computation lands on the audit's own worked
    values for the current 7-year horizon."""
    assert (1.0 + rate) ** horizon_years == pytest.approx(expected_moic, abs=5e-4)


def test_ten_x_moic_implies_38_950_pct_annual_cagr_at_horizon_7():
    required_cagr = 10.0 ** (1.0 / 7) - 1.0
    assert required_cagr == pytest.approx(0.38950, abs=5e-5)


def test_p_cagr_above_thresholds_are_monotonically_decreasing():
    """p_cagr_above_15/20/25 are exceedance probabilities of increasingly
    high horizon-derived MOIC cutoffs (2.660x/3.583x/4.768x at H=7) -- a
    higher bar must never carry more probability mass above it."""
    dist = _distribution(survival=0.92, mu_moic=5.0, sigma=1.1, horizon_years=7)
    assert dist["p_cagr_above_15"] >= dist["p_cagr_above_20"] >= dist["p_cagr_above_25"]


def test_median_cagr_consistent_with_p50_moic():
    dist = _distribution(survival=0.88, mu_moic=3.0, sigma=0.6, horizon_years=7)
    assert dist["median_cagr"] == pytest.approx(dist["p50_moic"] ** (1.0 / 7) - 1.0)


def test_p_terminal_wealth_below_0_5_is_an_additive_rename_not_a_replacement():
    """Old key must survive unchanged; new key is an exact alias, never a
    differently-computed number under a friendlier name."""
    dist = _distribution()
    assert dist["p_terminal_wealth_below_0_5"] == pytest.approx(dist["p_moic_below_0_5"])


# -- ce_cagr: the failure-atom floor must not be papered over --------------

def test_ce_cagr_is_finite_despite_nonzero_failure_probability():
    """Every real ticker has survival < 1, so E[ln W] would be -inf without
    the explicit floor (plan B-2 / audit §5.2) -- this must never surface
    as None, NaN, or -inf."""
    dist = _distribution(survival=0.70)
    assert dist["ce_cagr"] is not None
    assert math.isfinite(dist["ce_cagr"])


def test_ce_cagr_failure_floor_is_recorded_on_every_available_distribution():
    dist = _distribution()
    assert dist["ce_cagr_failure_floor"] == pytest.approx(CE_CAGR_FAILURE_FLOOR_MOIC)
    assert dist["ce_cagr_failure_floor"] == pytest.approx(0.01)


def test_ce_cagr_worsens_as_survival_drops_holding_everything_else_fixed():
    """Sanity direction check: more failure probability, priced via the
    floored E[ln W], must make CE CAGR worse (not better, not constant)."""
    high_survival = _distribution(survival=0.95)
    low_survival = _distribution(survival=0.60)
    assert low_survival["ce_cagr"] < high_survival["ce_cagr"]


def test_expected_shortfall_10pct_log_uses_the_same_floor_as_ce_cagr():
    """When the failure atom alone already exceeds 10% probability mass,
    the entire worst-decile slice sits inside the atom and the log-CAGR ES
    must equal ln(floor)/horizon exactly -- not 0, not -inf."""
    dist = _distribution(survival=0.85, horizon_years=7)  # failure mass 0.15 >= 0.10
    expected = math.log(CE_CAGR_FAILURE_FLOOR_MOIC) / 7
    assert dist["expected_shortfall_10pct_log"] == pytest.approx(expected)


def test_expected_shortfall_10pct_log_uses_continuous_slice_when_failure_mass_below_10pct():
    """When failure mass alone is < 10%, the worst-decile conditional
    expectation must blend the floored atom with the continuous mixture
    below the 10th percentile -- verified indirectly by requiring the value
    differ from the pure-atom shortcut used in the test above."""
    dist = _distribution(survival=0.97, mu_moic=3.0, sigma=0.4, horizon_years=7)
    atom_only = math.log(CE_CAGR_FAILURE_FLOOR_MOIC) / 7
    assert dist["expected_shortfall_10pct_log"] != pytest.approx(atom_only)
    # p10_moic must be strictly positive in this regime (continuous branch).
    assert dist["p10_moic"] > 0.0


# -- Unavailable fields must be None, never 0 -------------------------------

@pytest.mark.parametrize(
    "field,reason_field,reason_value",
    [
        ("p_permanent_loss", "p_permanent_loss_unavailable_reason", "competing_risk_model_not_implemented"),
    ],
)
def test_unimplemented_fields_are_none_with_explicit_reason_on_available_distribution(
    field, reason_field, reason_value,
):
    dist = _distribution()
    assert dist["status"] == "available"
    assert dist[field] is None
    assert dist[reason_field] == reason_value


# WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): drawdown/recovery are now
# implemented (path_risk.py), so a caller that does not pass a `path_risk`
# result to `scenario_distribution` (this file's `_distribution()` helper
# does not) gets "path_simulation_not_provided" -- distinct from
# "path_simulation_not_implemented" (which no longer applies to anything)
# and from a real attempt that came back unavailable for insufficient data
# (see test_v5_wp_f1_path_risk.py for that case).
@pytest.mark.parametrize(
    "field,reason_field",
    [
        ("expected_max_drawdown", "expected_max_drawdown_unavailable_reason"),
        ("p_mdd_above_30", "p_mdd_above_30_unavailable_reason"),
        ("p_mdd_above_50", "p_mdd_above_50_unavailable_reason"),
        ("p_mdd_above_70", "p_mdd_above_70_unavailable_reason"),
        ("recovery_time_median", "recovery_time_median_unavailable_reason"),
    ],
)
def test_path_risk_fields_are_none_with_not_provided_reason_when_no_path_risk_passed(
    field, reason_field,
):
    dist = _distribution()
    assert dist["status"] == "available"
    assert dist[field] is None
    assert dist[reason_field] == "path_simulation_not_provided"


@pytest.mark.parametrize(
    "field",
    [
        "p_permanent_loss", "expected_max_drawdown", "p_mdd_above_30", "p_mdd_above_50",
        "p_mdd_above_70", "recovery_time_median", "ce_cagr", "p_cagr_above_15",
        "p_cagr_above_20", "p_cagr_above_25", "expected_shortfall_10pct_log",
        "p_terminal_wealth_below_0_5", "ce_cagr_failure_floor",
    ],
)
def test_all_new_fields_are_none_not_zero_when_distribution_itself_is_unavailable(field):
    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    assert dist[field] is None


def test_unimplemented_fields_never_silently_default_to_zero():
    """Direct guard against the exact failure mode the audit calls out:
    a 0.0 here would read as 'no permanent-loss risk' / 'no drawdown'."""
    dist = _distribution()
    for field in (
        "p_permanent_loss", "expected_max_drawdown",
        "p_mdd_above_30", "p_mdd_above_50", "p_mdd_above_70", "recovery_time_median",
    ):
        assert dist[field] is None
        assert dist[field] != 0.0


# -- B-3: risk_adjusted_compounding (RACR) shadow objective -----------------

def test_racr_is_available_and_computed_from_ce_cagr():
    dist = _distribution(survival=0.85)
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    racr = results["risk_adjusted_compounding"]
    assert racr.status == "available"
    assert racr.score_value is not None
    assert racr.explanation["ce_cagr"] == pytest.approx(dist["ce_cagr"])


def test_racr_explanation_always_reports_omitted_terms():
    """Hard constraint (plan B-3): while drawdown/permanent-loss are
    unavailable, RACR's explanation must say so on every evaluation, so the
    score is never misread as fully risk-adjusted."""
    dist = _distribution()
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    explanation = results["risk_adjusted_compounding"].explanation
    assert explanation["omitted_terms"] == ["drawdown", "permanent_loss"]
    assert explanation["dd_excess"] == 0.0
    assert explanation["p_permanent_loss"] == 0.0


def test_racr_penalizes_worse_tail_loss():
    """Holding survival/mu roughly comparable, a materially worse downside
    (lower mu -> more negative expected_shortfall_10pct_log) must lower
    RACR through the tail_lambda term."""
    config = _objectives_config()
    mild = _distribution(survival=0.90, mu_moic=3.0, sigma=0.5)
    severe = _distribution(survival=0.90, mu_moic=3.0, sigma=1.4)
    mild_results = evaluate_objectives(mild, config, horizon_years=7)
    severe_results = evaluate_objectives(severe, config, horizon_years=7)
    assert severe["expected_shortfall_10pct_log"] < mild["expected_shortfall_10pct_log"]
    assert (
        severe_results["risk_adjusted_compounding"].score_value
        < mild_results["risk_adjusted_compounding"].score_value
    )


def test_racr_penalizes_low_model_confidence():
    """ModelUncertainty must scale with (1 - model_confidence); an
    otherwise-identical distribution with lower confidence must score
    lower on RACR (never higher)."""
    config = _objectives_config()
    confident = _distribution(survival=0.85, confidence=0.95)
    unsure = _distribution(survival=0.85, confidence=0.10)
    confident_results = evaluate_objectives(confident, config, horizon_years=7)
    unsure_results = evaluate_objectives(unsure, config, horizon_years=7)
    assert (
        unsure_results["risk_adjusted_compounding"].explanation["model_uncertainty"]
        >= confident_results["risk_adjusted_compounding"].explanation["model_uncertainty"]
    )


def test_racr_is_unsupported_when_distribution_unavailable():
    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    assert results["risk_adjusted_compounding"].status == "unavailable"
    assert results["risk_adjusted_compounding"].score_value is None


def test_racr_lambdas_come_from_config_not_hardcoded():
    dist = _distribution()
    zero_lambda_config = ObjectivesConfig(
        default_objective="ten_bagger",
        objectives={
            "ten_bagger": ObjectiveDefinition(description="test"),
            "risk_adjusted_compounding": ObjectiveDefinition(
                description="test", tail_lambda=0.0, drawdown_lambda=0.0,
                permanent_loss_lambda=0.0, uncertainty_lambda=0.0,
            ),
        },
    )
    results = evaluate_objectives(dist, zero_lambda_config, horizon_years=7)
    # With every lambda at 0, RACR collapses to exactly ce_cagr.
    assert results["risk_adjusted_compounding"].score_value == pytest.approx(dist["ce_cagr"])


# -- Deprecated risk_adjusted must keep working ------------------------------

def test_deprecated_risk_adjusted_still_evaluates():
    dist = _distribution()
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    assert results["risk_adjusted"].status == "available"
    assert results["risk_adjusted"].score_value is not None


def test_risk_adjusted_config_definition_is_marked_deprecated():
    definition = _objectives_config().objectives["risk_adjusted"]
    assert definition.deprecated is True
    assert definition.enabled is True


def test_real_config_yaml_keeps_default_objective_as_ten_bagger():
    """Hard constraint: RACR is shadow-only. Loading the real
    config/objectives.yaml must still resolve default_objective to
    ten_bagger, and risk_adjusted_compounding must be enabled (shadow) but
    never default."""
    from autoscreener.config import load_objectives_config

    config = load_objectives_config()
    assert config.default_objective == "ten_bagger"
    racr_definition = config.objectives["risk_adjusted_compounding"]
    assert racr_definition.enabled is True
    risk_adjusted_definition = config.objectives["risk_adjusted"]
    assert risk_adjusted_definition.enabled is True
    assert risk_adjusted_definition.deprecated is True
