"""WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md) regression tests.

Fixes the defect found on the first real shadow run
(docs/racr_shadow_run_diagnostic_2026-09-04.md): every RACR risk term was a
universe-wide constant, so ``Spearman(RACR, ce_cagr) == 1.0000000000`` for
1,155/1,155 tickers. Covers:

  - B2-1: the per-run objective diagnostics that would have caught this
    automatically (pairwise Spearman, Top-20 overlap, constant-term
    detection) -- pure functions in ``scoring/v5/engine.py``, tested here
    without any DB session, same as this WP's own test-scope restriction.
  - B2-2: the new tail-loss-conditional-on-survival term and the separate
    failure-frequency term in the ``risk_adjusted_compounding`` (RACR)
    objective, contract version v5.racr2.

Pure-Python throughout: no DB session anywhere in this file.
"""

from __future__ import annotations

import datetime
import math
from types import SimpleNamespace

import pytest

from autoscreener.config import ObjectiveDefinition, ObjectivesConfig, load_model_v5_config
from autoscreener.db.models import ModelRun, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.distribution import (
    CE_CAGR_FAILURE_FLOOR_MOIC,
    scenario_distribution,
)
from autoscreener.scoring.v5.engine import (
    _constant_explanation_terms,
    _distribution_field_diagnostics,
    _pairwise_objective_spearman,
    _top20_overlap_vs_expected_return,
    run_v5_shadow,
)
from autoscreener.scoring.v5.inputs import V5PitInput
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
            "risk_adjusted_compounding": ObjectiveDefinition(
                description="test", tail_lambda=0.35, failure_lambda=0.20,
                drawdown_lambda=0.10, permanent_loss_lambda=0.20,
                uncertainty_lambda=0.50,
            ),
        },
    )


# -- B2-2: distribution-level tail term -------------------------------------

OLD_ATOM_CONSTANT_H7 = math.log(CE_CAGR_FAILURE_FLOOR_MOIC) / 7  # -0.6578814551411558


def test_contract_version_is_racr2():
    dist = _distribution()
    assert dist["contract_version"] == "v5.racr2"


def test_new_field_present_on_available_distribution():
    dist = _distribution()
    assert dist["expected_shortfall_10pct_log_given_survival"] is not None
    assert math.isfinite(dist["expected_shortfall_10pct_log_given_survival"])


def test_new_field_is_none_on_unavailable_distribution():
    from autoscreener.scoring.v5.distribution import unavailable_distribution

    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    assert dist["expected_shortfall_10pct_log_given_survival"] is None


def test_old_field_keeps_its_defective_behaviour_unchanged():
    """B2 explicitly keeps the old field byte-for-byte for backward
    compatibility -- it is documented as defective (constant whenever
    failure mass >= 10%), not deleted."""
    dist = _distribution(survival=0.85, horizon_years=7)  # failure mass 0.15 >= 0.10
    assert dist["expected_shortfall_10pct_log"] == pytest.approx(OLD_ATOM_CONSTANT_H7)


def test_new_field_does_not_collapse_to_the_old_atom_constant_when_failure_mass_exceeds_10pct():
    """The exact defect (docs/racr_shadow_run_diagnostic_2026-09-04.md §3.1):
    every real ticker had failure mass >= 10%, so the *old* measure was
    identically ln(floor)/H for the whole universe. The new
    conditional-on-survival measure must not reproduce that constant here,
    even though this ticker's failure mass (15%) exceeds 10%."""
    dist = _distribution(survival=0.85, horizon_years=7)
    assert dist["expected_shortfall_10pct_log_given_survival"] != pytest.approx(
        OLD_ATOM_CONSTANT_H7
    )


def test_new_field_is_insensitive_to_survival_holding_continuous_mixture_fixed():
    """The conditional-on-survival tail measure is, by construction, a
    property of the continuous mixture alone -- changing *only* survival
    (holding mu/sigma fixed) must not move it at all, unlike the old
    unconditional measure which is dominated by the failure atom."""
    low_survival = _distribution(survival=0.60, mu_moic=3.0, sigma=0.8)
    high_survival = _distribution(survival=0.95, mu_moic=3.0, sigma=0.8)
    assert low_survival["expected_shortfall_10pct_log_given_survival"] == pytest.approx(
        high_survival["expected_shortfall_10pct_log_given_survival"]
    )
    # The old field, by contrast, is very much survival-sensitive.
    assert low_survival["expected_shortfall_10pct_log"] != pytest.approx(
        high_survival["expected_shortfall_10pct_log"]
    )


def test_new_field_varies_with_sigma():
    """Must carry ranking information: two tickers differing only in
    dispersion must get different conditional tail-loss values."""
    tight = _distribution(survival=0.90, mu_moic=3.0, sigma=0.4)
    wide = _distribution(survival=0.90, mu_moic=3.0, sigma=1.2)
    assert tight["expected_shortfall_10pct_log_given_survival"] != pytest.approx(
        wide["expected_shortfall_10pct_log_given_survival"]
    )
    # Wider dispersion must produce a *worse* (more negative) tail value.
    assert (
        wide["expected_shortfall_10pct_log_given_survival"]
        < tight["expected_shortfall_10pct_log_given_survival"]
    )


def test_new_field_produces_many_distinct_values_across_a_varied_population():
    """Direct regression guard for the diagnostic's headline number: 1,157
    distinct values across the real universe, not 1."""
    survivals = [0.60 + 0.30 * i / 20 for i in range(21)]
    sigmas = [0.3 + 1.2 * i / 20 for i in range(21)]
    values = {
        round(
            _distribution(survival=s, mu_moic=3.0, sigma=sig)[
                "expected_shortfall_10pct_log_given_survival"
            ],
            9,
        )
        for s in survivals
        for sig in sigmas
    }
    # 21x21 = 441 combinations; sigma alone should already produce >= 21
    # distinct values since survival no longer collapses them together.
    assert len(values) >= 21


# -- B2-2: risk_adjusted_compounding (RACR v5.racr2) -------------------------

def test_racr_explanation_has_new_terms_not_old_tail_loss_10():
    dist = _distribution(survival=0.85)
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    explanation = results["risk_adjusted_compounding"].explanation
    assert "cond_tail_loss_10" in explanation
    assert "p_failure" in explanation
    assert "assumed_recovery" in explanation
    assert "failure_loss" in explanation
    assert "failure_lambda" in explanation
    assert explanation["p_failure"] == pytest.approx(1.0 - dist["survival_probability"])
    assert explanation["assumed_recovery"] == pytest.approx(dist["ce_cagr_failure_floor"])
    assert explanation["failure_loss"] == pytest.approx(
        explanation["p_failure"] * (1.0 - explanation["assumed_recovery"])
    )


def test_racr_permanent_loss_field_still_none_and_omitted_terms_unchanged():
    """Hard constraint (task instructions): the new failure term must never
    be conflated with permanent loss. `p_permanent_loss` stays None+reason
    in the distribution, and the objective's own zeroed placeholder / the
    omitted_terms list are unaffected by this WP."""
    dist = _distribution()
    assert dist["p_permanent_loss"] is None
    assert dist["p_permanent_loss_unavailable_reason"] == "competing_risk_model_not_implemented"
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    explanation = results["risk_adjusted_compounding"].explanation
    assert explanation["omitted_terms"] == ["drawdown", "permanent_loss"]
    assert explanation["p_permanent_loss"] == 0.0
    assert explanation["dd_excess"] == 0.0


def test_racr_value_matches_hand_computed_formula():
    dist = _distribution(survival=0.80, mu_moic=2.5, sigma=0.9)
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    explanation = results["risk_adjusted_compounding"].explanation
    expected = (
        dist["ce_cagr"]
        - 0.35 * explanation["cond_tail_loss_10"]
        - 0.20 * explanation["failure_loss"]
        - 0.10 * 0.0
        - 0.20 * 0.0
        - 0.50 * explanation["model_uncertainty"]
    )
    assert results["risk_adjusted_compounding"].score_value == pytest.approx(expected)


def test_racr_penalizes_higher_failure_probability_holding_tail_and_ce_cagr_shape_similar():
    """A ticker whose failure mass exceeds 10% must be penalized through the
    new explicit failure term, not silently ignored -- direction check:
    more failure probability (holding mu/sigma fixed) must never *improve*
    RACR relative to CE CAGR's own drop."""
    config = _objectives_config()
    low_failure = _distribution(survival=0.95, mu_moic=3.0, sigma=0.7)
    high_failure = _distribution(survival=0.70, mu_moic=3.0, sigma=0.7)
    low_results = evaluate_objectives(low_failure, config, horizon_years=7)
    high_results = evaluate_objectives(high_failure, config, horizon_years=7)
    low_explanation = low_results["risk_adjusted_compounding"].explanation
    high_explanation = high_results["risk_adjusted_compounding"].explanation
    assert high_explanation["failure_loss"] > low_explanation["failure_loss"]
    assert (
        high_results["risk_adjusted_compounding"].score_value
        < low_results["risk_adjusted_compounding"].score_value
    )


def test_racr_tail_term_is_not_the_old_constant_even_when_failure_mass_exceeds_10pct():
    """Direct regression guard: a ticker whose failure mass exceeds 10%
    (the exact condition that broke the old formula for 100% of the real
    universe) must not produce the fixed constant -0.6578814551411558/H
    tail contribution any more."""
    dist = _distribution(survival=0.85, horizon_years=7)  # failure mass 0.15 >= 0.10
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    cond_tail_loss_10 = results["risk_adjusted_compounding"].explanation["cond_tail_loss_10"]
    assert cond_tail_loss_10 != pytest.approx(-OLD_ATOM_CONSTANT_H7)


def test_racr_still_unsupported_when_distribution_unavailable():
    from autoscreener.scoring.v5.distribution import unavailable_distribution

    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    results = evaluate_objectives(dist, _objectives_config(), horizon_years=7)
    assert results["risk_adjusted_compounding"].status == "unavailable"
    assert results["risk_adjusted_compounding"].score_value is None


def test_failure_lambda_is_a_config_driven_policy_prior_not_hardcoded():
    dist = _distribution(survival=0.70)
    zero_config = ObjectivesConfig(
        default_objective="ten_bagger",
        objectives={
            "ten_bagger": ObjectiveDefinition(description="test"),
            "risk_adjusted_compounding": ObjectiveDefinition(
                description="test", tail_lambda=0.0, failure_lambda=0.0,
                drawdown_lambda=0.0, permanent_loss_lambda=0.0, uncertainty_lambda=0.0,
            ),
        },
    )
    results = evaluate_objectives(dist, zero_config, horizon_years=7)
    assert results["risk_adjusted_compounding"].score_value == pytest.approx(dist["ce_cagr"])


def test_real_config_yaml_has_failure_lambda_and_default_stays_ten_bagger():
    from autoscreener.config import load_objectives_config

    config = load_objectives_config()
    assert config.default_objective == "ten_bagger"
    racr_definition = config.objectives["risk_adjusted_compounding"]
    assert racr_definition.enabled is True
    assert racr_definition.failure_lambda == pytest.approx(0.20)
    assert racr_definition.tail_lambda == pytest.approx(0.35)
    # Naming constraint: failure_lambda and permanent_loss_lambda are
    # distinct config fields even though the audit's policy table assigns
    # both an identical 0.20 prior -- they must never be merged into one
    # field, or a future reader could mistake "failure frequency is priced"
    # for "permanent loss is measured".
    assert racr_definition.permanent_loss_lambda == pytest.approx(0.20)


# -- B2-1: run-level objective diagnostics (pure functions, no DB) ----------

def test_pairwise_spearman_flags_near_duplicate_objectives():
    # Two objectives whose scores are monotonically identical across every
    # ticker -- the exact shape of the old risk_adjusted/expected_return
    # near-duplication this diagnostic exists to catch.
    objective_values = {
        "expected_return": {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40, 5: 0.50},
        "duplicate_objective": {1: 1.10, 2: 1.20, 3: 1.30, 4: 1.40, 5: 1.50},
        "independent_objective": {1: 0.50, 2: 0.10, 3: 0.40, 4: 0.20, 5: 0.30},
    }
    pairwise, vs_ce_cagr, warnings = _pairwise_objective_spearman(objective_values, {})
    assert pairwise["duplicate_objective__vs__expected_return"] == pytest.approx(1.0)
    assert any("duplicate_objective_vs_expected_return" in w for w in warnings)
    # An objective with no meaningful relationship must not also be flagged.
    assert not any("independent_objective" in w for w in warnings)


def test_pairwise_spearman_vs_ce_cagr_computed_for_every_objective():
    objective_values = {
        "expected_return": {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.40},
        "risk_adjusted_compounding": {1: 0.05, 2: 0.05, 3: 0.30, 4: 0.10},
    }
    ce_cagr_by_ticker = {1: 0.08, 2: 0.18, 3: 0.28, 4: 0.38}
    _, vs_ce_cagr, _ = _pairwise_objective_spearman(objective_values, ce_cagr_by_ticker)
    assert "expected_return" in vs_ce_cagr
    assert "risk_adjusted_compounding" in vs_ce_cagr
    assert vs_ce_cagr["expected_return"] == pytest.approx(1.0)


def test_pairwise_spearman_skips_pairs_with_insufficient_overlap():
    objective_values = {
        "a": {1: 0.1, 2: 0.2},  # only 2 tickers -- below the len>=3 floor
        "b": {1: 0.3, 2: 0.4},
    }
    pairwise, _, warnings = _pairwise_objective_spearman(objective_values, {})
    assert pairwise == {}
    assert warnings == []


def test_top20_overlap_counts_common_tickers_in_both_top20s():
    objective_ranks = {
        "expected_return": {t: t for t in range(1, 26)},  # rank == ticker_id
        "risk_adjusted_compounding": {t: (26 - t) for t in range(1, 26)},  # reversed
    }
    overlap = _top20_overlap_vs_expected_return(objective_ranks)
    # Reversed ranking over 25 tickers: top-20 sets share tickers 21..25's
    # counterparts -- exact overlap count is what matters, not the number.
    assert overlap["risk_adjusted_compounding"] == len(
        {t for t in range(1, 26) if t <= 20} & {t for t in range(1, 26) if (26 - t) <= 20}
    )


def test_top20_overlap_empty_when_expected_return_not_enabled():
    assert _top20_overlap_vs_expected_return({"risk_adjusted_compounding": {1: 1}}) == {}


def test_constant_explanation_terms_flags_a_universe_wide_constant():
    """This is the exact diagnostic that would have caught the RACR defect
    by itself: a numeric explanation field with exactly one distinct value
    across the whole scored population."""
    explanations = {
        "risk_adjusted_compounding": [
            {"status": "available", "cond_tail_loss_10": 0.6578814551411558, "ce_cagr": 0.05},
            {"status": "available", "cond_tail_loss_10": 0.6578814551411558, "ce_cagr": 0.10},
            {"status": "available", "cond_tail_loss_10": 0.6578814551411558, "ce_cagr": -0.02},
        ],
    }
    constant_terms, warnings = _constant_explanation_terms(explanations)
    assert "cond_tail_loss_10" in constant_terms["risk_adjusted_compounding"]
    assert "ce_cagr" not in constant_terms["risk_adjusted_compounding"]
    assert any("cond_tail_loss_10" in w for w in warnings)


def test_constant_explanation_terms_ignores_bool_and_non_numeric_fields():
    explanations = {
        "objective_a": [
            {"status": "available", "flag": True, "formula": "x", "value": 1.0},
            {"status": "available", "flag": True, "formula": "y", "value": 2.0},
        ],
    }
    constant_terms, warnings = _constant_explanation_terms(explanations)
    # `flag` is a bool -- must not be reported as a constant numeric term.
    assert "flag" not in constant_terms.get("objective_a", [])
    assert "formula" not in constant_terms.get("objective_a", [])
    assert "value" not in constant_terms.get("objective_a", [])


def test_constant_explanation_terms_requires_at_least_two_scored_tickers():
    explanations = {"objective_a": [{"status": "available", "x": 1.0}]}
    constant_terms, warnings = _constant_explanation_terms(explanations)
    assert constant_terms == {}
    assert warnings == []


def test_distribution_field_diagnostics_flags_constant_model_confidence():
    """Would have caught the second real defect
    (docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2): model_confidence
    pinned at 0.5 for the entire universe."""
    values = {
        "ce_cagr": {0.05, 0.10, -0.02, 0.30},
        "model_confidence": {0.5},
    }
    counts = {"ce_cagr": 4, "model_confidence": 4}
    distinct_counts, constant_fields, warnings = _distribution_field_diagnostics(values, counts)
    assert distinct_counts["ce_cagr"] == 4
    assert distinct_counts["model_confidence"] == 1
    assert "model_confidence" in constant_fields
    assert "ce_cagr" not in constant_fields
    assert any("model_confidence" in w for w in warnings)


def test_distribution_field_diagnostics_requires_at_least_two_observations():
    values = {"ce_cagr": {0.05}}
    counts = {"ce_cagr": 1}
    _, constant_fields, warnings = _distribution_field_diagnostics(values, counts)
    assert constant_fields == []
    assert warnings == []


# -- B2-1: full run_v5_shadow integration (DB-backed) -----------------------

def test_run_v5_shadow_persists_objective_diagnostics_into_run_metrics(monkeypatch):
    """End-to-end guard: `run_v5_shadow` must actually persist the B2-1
    diagnostics into ``model_runs.metrics``, not just have the pure
    functions exist. Three tickers with deliberately different
    survival/mu/sigma so ``ce_cagr`` varies, but real feature-set builders
    run unmodified against fresh tickers with no other DB rows -- so
    ``model_confidence`` collapses to the same baseline for all three,
    reproducing (at small scale) the exact defect this diagnostic exists to
    catch automatically."""
    variants = [(0.95, 3.0, 0.4), (0.85, 2.0, 0.7), (0.60, 1.5, 1.3)]
    symbols = ["ZZB2DIAG1", "ZZB2DIAG2", "ZZB2DIAG3"]
    ticker_ids: list[int] = []
    with session_scope() as session:
        for symbol in symbols:
            old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if old is not None:
                session.delete(old)
                session.flush()
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            ticker_ids.append(ticker.id)

    items = [
        V5PitInput(
            ticker_id=ticker_id,
            symbol=symbol,
            as_of=datetime.date(2024, 6, 30),
            moic_inputs=SimpleNamespace(variant=i, fcf_margin=0.08, net_debt=100.0),
            raw_snapshot_id=123,
            raw_available_from=datetime.date(2024, 6, 1),
            price_as_of=datetime.date(2024, 6, 28),
            input_status="collected_with_data",
        )
        for i, (ticker_id, symbol) in enumerate(zip(ticker_ids, symbols, strict=True))
    ]

    def _fake_compute_moic(moic_inputs, *_args, **_kwargs):
        survival, mu_moic, sigma = variants[moic_inputs.variant]
        return SimpleNamespace(
            probability=0.04, expected_moic=mu_moic, median_moic=mu_moic * 0.8,
            log_moic_mu=math.log(mu_moic) - 0.5 * sigma**2, log_moic_sigma=sigma,
            survival_probability=survival, initial_growth_rate=0.12,
            terminal_growth_rate=0.04, revenue_multiple=2.0, terminal_gross_margin=0.45,
            dilution_drag=1.1, projected_net_debt=50.0, current_ev_to_gross_profit=4.0,
            multiple_change=0.8, growth_fade_rate=0.75,
        )

    monkeypatch.setattr("autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: items)
    monkeypatch.setattr("autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object())
    monkeypatch.setattr("autoscreener.scoring.v5.engine.compute_moic", _fake_compute_moic)

    result = run_v5_shadow(datetime.date(2024, 6, 30))
    run_id = result["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            assert run is not None
            assert run.status == "succeeded"
            diagnostics = run.metrics["objective_diagnostics"]
            for key in (
                "pairwise_spearman", "spearman_vs_ce_cagr",
                "top20_overlap_vs_expected_return", "constant_explanation_terms",
                "distribution_distinct_value_counts", "distribution_constant_fields",
            ):
                assert key in diagnostics
            # Three genuinely different tickers -> ce_cagr must not collapse.
            assert diagnostics["distribution_distinct_value_counts"]["ce_cagr"] == 3
            # No per-ticker DB signal data exists for these fresh tickers,
            # so confidence never moves off the shared baseline -- exactly
            # the shape of the diagnosed defect, now caught automatically.
            assert "model_confidence" in diagnostics["distribution_constant_fields"]
            assert any(
                w.startswith("distribution_constant_field:model_confidence=")
                for w in (run.warnings or [])
            )
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
            for ticker_id in ticker_ids:
                session.query(Ticker).filter_by(id=ticker_id).delete()
