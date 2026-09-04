"""WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md) regression tests.

Three prior work packages (WP-B2, WP-D) tried to give
``risk_adjusted_compounding`` (RACR) independent risk information and each
failed because every risk term was derived from the same V4 lognormal
seed's mu/sigma/survival, collinear with ``ce_cagr`` by construction
(docs/racr_shadow_run_diagnostic_2026-09-04.md §6). This file covers:

  - F1-1: ``scoring/v5/path_risk.py``'s pure block-bootstrap
    historical-simulation estimator -- insufficient-history handling, PIT
    (no post-``as_of`` price row can influence a result), and that two
    tickers with genuinely different realized volatility get genuinely
    different ``expected_max_drawdown`` (the guard against the exact
    collapse-to-a-constant failure mode the three prior WPs hit).
  - F1-2: the distribution contract (``v5.racr3``) and RACR objective
    wiring -- ``expected_max_drawdown``/``p_mdd_above_30/50/70``/
    ``expected_drawdown_excess_35``/recovery fields populate from a real
    ``PathRiskResult``; ``permanent_loss`` stays in ``omitted_terms``
    unconditionally; ``p_permanent_loss`` stays ``None`` + its own reason.
  - F1-3: feature registry freshness half-lives are no longer universally
    dead metadata.
  - An end-to-end ``run_v5_shadow`` regression guard mirroring
    ``test_v5_wp_d_reliability.py``'s style: two tickers with genuinely
    different realized price history must not collapse to the same
    ``expected_max_drawdown``.
"""

from __future__ import annotations

import datetime
import math
import uuid
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.db.models import ModelFeatureValue, ModelRun, ModelScore, ObjectiveScore, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.distribution import (
    scenario_distribution,
    unavailable_distribution,
)
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.v5.feature_registry import FEATURE_REGISTRY
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.path_risk import (
    MIN_DAILY_OBSERVATIONS,
    PathRiskResult,
    PriceObservation,
    _max_drawdown_and_recovery,
    _weekly_total_returns,
    estimate_path_risk,
    stable_seed,
)
from autoscreener.scoring.v5.scenario import build_scenarios

AS_OF = datetime.date(2024, 6, 30)


def _observations(closes: list[float], *, start: datetime.date) -> list[PriceObservation]:
    return [
        PriceObservation(trade_date=start + datetime.timedelta(days=i), close=close)
        for i, close in enumerate(closes)
    ]


def _flat_series(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def _crash_cycle_series(n: int) -> list[float]:
    """Deterministic repeating pattern: rise for 90 days, crash ~55% over
    20 days, recover over 60 days -- long enough (170-day period) that a
    4-week bootstrap block reliably samples pieces of both the crash and
    the recovery across a multi-year simulated horizon."""
    closes: list[float] = []
    price = 100.0
    period = 170
    for i in range(n):
        phase = i % period
        if phase < 90:
            price *= 1.010  # steady rise
        elif phase < 110:
            price *= 0.960  # sharp decline (~55% over 20 days)
        else:
            price *= 1.014  # recovery
        closes.append(price)
    return closes


# ---------------------------------------------------------------------------
# F1-1: pure math helpers
# ---------------------------------------------------------------------------

def test_weekly_total_returns_aggregates_five_days_compounded():
    start = datetime.date(2024, 1, 1)
    # Two full weeks (10 daily observations -> 9 daily returns -> 1 full
    # 5-return week, remainder dropped).
    closes = [100.0, 101.0, 102.0, 101.5, 103.0, 104.0, 103.0, 105.0, 106.0, 107.0]
    obs = _observations(closes, start=start)
    weekly = _weekly_total_returns(obs)
    assert len(weekly) == 1
    expected = 1.0
    for prev, cur in zip(closes[:5], closes[1:6]):
        expected *= cur / prev
    assert weekly[0] == pytest.approx(expected - 1.0)


def test_weekly_total_returns_adds_dividend_to_price_return():
    start = datetime.date(2024, 1, 1)
    obs = [
        PriceObservation(trade_date=start, close=100.0),
        PriceObservation(trade_date=start + datetime.timedelta(days=1), close=100.0, dividend=1.0),
    ] + [
        PriceObservation(trade_date=start + datetime.timedelta(days=i), close=100.0)
        for i in range(2, 6)
    ]
    weekly = _weekly_total_returns(obs)
    assert len(weekly) == 1
    # First daily return: (100 + 1)/100 - 1 = 0.01; remaining four are 0.
    assert weekly[0] == pytest.approx(1.01 - 1.0)


def test_max_drawdown_and_recovery_known_path():
    # +10%, -50%, +10%, +90% (recovers past the pre-drawdown peak by the end)
    returns = [0.10, -0.50, 0.10, 0.90]
    mdd, recovery_weeks = _max_drawdown_and_recovery(returns)
    # peak after week 1 = 1.10; trough after week 2 = 1.10*0.5=0.55 -> MDD=0.5
    assert mdd == pytest.approx(0.5)
    # wealth path: 1.0, 1.10, 0.55, 0.605, 1.1495 -- recovers to >=1.10 at index 4
    # (trough at index 2), so recovery_weeks == 4 - 2 == 2.
    assert recovery_weeks == 2


def test_max_drawdown_and_recovery_never_recovers_within_path():
    returns = [0.10, -0.50, 0.01, 0.01]
    mdd, recovery_weeks = _max_drawdown_and_recovery(returns)
    assert mdd == pytest.approx(0.5)
    assert recovery_weeks is None


def test_max_drawdown_and_recovery_no_drawdown_is_zero_with_zero_recovery():
    mdd, recovery_weeks = _max_drawdown_and_recovery([0.01, 0.02, 0.01])
    assert mdd == 0.0
    assert recovery_weeks == 0


# ---------------------------------------------------------------------------
# F1-1: estimate_path_risk -- insufficient history, never fabricates 0
# ---------------------------------------------------------------------------

def test_insufficient_history_is_unavailable_not_zero():
    obs = _observations(_flat_series(100), start=datetime.date(2020, 1, 1))
    result = estimate_path_risk(obs, as_of=datetime.date(2020, 4, 15), horizon_years=7)
    assert result.status == "unavailable"
    assert result.unavailable_reason == "insufficient_price_history"
    assert result.expected_max_drawdown is None
    assert result.p_mdd_above_30 is None
    assert result.dd_excess is None
    assert result.recovery_time_median_days is None


def test_exactly_at_minimum_boundary_is_still_insufficient():
    """MIN_DAILY_OBSERVATIONS is a lower bound on the *count required
    before* this ticker is trusted -- exactly at the boundary (not one more)
    must still be unavailable, guarding an off-by-one that would silently
    admit a too-short sample."""
    obs = _observations(_flat_series(MIN_DAILY_OBSERVATIONS), start=datetime.date(2018, 1, 1))
    as_of = obs[-1].trade_date
    result = estimate_path_risk(obs, as_of=as_of, horizon_years=7)
    assert result.status == "unavailable"


def test_flat_price_series_has_zero_drawdown_available():
    """A perfectly flat price series is long enough to be *available* --
    zero drawdown is a real measurement here, not a fallback."""
    obs = _observations(_flat_series(600), start=datetime.date(2018, 1, 1))
    as_of = obs[-1].trade_date
    result = estimate_path_risk(obs, as_of=as_of, horizon_years=7, simulations=50, seed=1)
    assert result.status == "available"
    assert result.expected_max_drawdown == pytest.approx(0.0, abs=1e-9)
    assert result.p_mdd_above_30 == pytest.approx(0.0)
    assert result.dd_excess == pytest.approx(0.0)
    # No drawdown ever occurs, so recovery-time is honestly "no episodes to
    # measure", not a fabricated zero.
    assert result.recovery_time_median_days is None
    assert result.recovery_time_unavailable_reason == "insufficient_recoveries_within_horizon"


def test_crash_cycle_series_shows_material_drawdown_and_recovers():
    obs = _observations(_crash_cycle_series(900), start=datetime.date(2016, 1, 1))
    as_of = obs[-1].trade_date
    result = estimate_path_risk(obs, as_of=as_of, horizon_years=7, simulations=200, seed=42)
    assert result.status == "available"
    # Every ~170-day cycle carries a ~55% drawdown; over a 7-year simulated
    # horizon built from real historical blocks, most paths should see one.
    assert result.expected_max_drawdown > 0.20
    assert result.p_mdd_above_30 > 0.5
    assert result.dd_excess > 0.0
    # The pattern always recovers within a cycle, so recovery stats should
    # be reportable (not held back by the min-sample gate).
    assert result.recovery_time_median_days is not None
    assert result.recovery_time_median_days > 0.0
    assert result.recovery_time_p90_days >= result.recovery_time_median_days


# ---------------------------------------------------------------------------
# F1-1: PIT -- no observation after as_of may influence the result
# ---------------------------------------------------------------------------

def test_pit_future_observations_never_change_the_result():
    start = datetime.date(2015, 1, 1)
    history = _crash_cycle_series(700)
    as_of = start + datetime.timedelta(days=len(history) - 1)

    truncated = _observations(history, start=start)
    result_truncated = estimate_path_risk(
        truncated, as_of=as_of, horizon_years=7, simulations=80, seed=7,
    )

    # Same history, plus a wildly different tail *after* as_of (an extreme
    # crash to zero) that must have zero effect once PIT-filtered.
    future_crash = list(history) + [history[-1] * (0.5 ** i) for i in range(1, 200)]
    full = _observations(future_crash, start=start)
    result_with_future = estimate_path_risk(
        full, as_of=as_of, horizon_years=7, simulations=80, seed=7,
    )

    assert result_truncated.status == result_with_future.status == "available"
    assert result_truncated.observations_used == result_with_future.observations_used
    assert result_truncated.expected_max_drawdown == pytest.approx(
        result_with_future.expected_max_drawdown
    )
    assert result_truncated.p_mdd_above_70 == pytest.approx(result_with_future.p_mdd_above_70)


def test_pit_filters_observations_with_trade_date_after_as_of():
    """A caller that (incorrectly) hands this function rows beyond as_of
    still gets a PIT-safe answer -- ``estimate_path_risk`` re-filters
    defensively rather than trusting the caller."""
    start = datetime.date(2015, 1, 1)
    history = _crash_cycle_series(700)
    obs = _observations(history, start=start)
    as_of = start + datetime.timedelta(days=500)
    result = estimate_path_risk(obs, as_of=as_of, horizon_years=7, simulations=30, seed=3)
    assert result.observations_used == 501  # days 0..500 inclusive


# ---------------------------------------------------------------------------
# F1-1: distinctness guard -- must not collapse to a universe-wide constant
# ---------------------------------------------------------------------------

def test_two_tickers_with_different_realized_volatility_get_different_mdd():
    calm = _observations(_flat_series(700, price=50.0), start=datetime.date(2017, 1, 1))
    turbulent = _observations(_crash_cycle_series(700), start=datetime.date(2017, 1, 1))
    as_of = datetime.date(2018, 12, 1)
    calm_result = estimate_path_risk(calm, as_of=as_of, horizon_years=7, simulations=100, seed=11)
    turbulent_result = estimate_path_risk(
        turbulent, as_of=as_of, horizon_years=7, simulations=100, seed=11,
    )
    assert calm_result.status == turbulent_result.status == "available"
    assert turbulent_result.expected_max_drawdown - calm_result.expected_max_drawdown > 0.15


def test_stable_seed_differs_across_tickers_and_is_deterministic():
    a = stable_seed(1, AS_OF)
    b = stable_seed(2, AS_OF)
    assert a != b
    assert stable_seed(1, AS_OF) == a


# ---------------------------------------------------------------------------
# F1-2: distribution contract wiring (contract_version v5.racr3)
# ---------------------------------------------------------------------------

def _seed_result(survival: float = 0.85, *, mu_moic: float = 2.0, sigma: float = 0.7):
    return SimpleNamespace(
        log_moic_mu=math.log(mu_moic) - 0.5 * sigma**2,
        log_moic_sigma=sigma,
        survival_probability=survival,
    )


def _base_distribution(*, path_risk=None, horizon_years: int = 7, confidence: float = 0.5) -> dict:
    config = load_model_v5_config()
    scenarios = build_scenarios(_seed_result(), confidence=confidence, config=config)
    return scenario_distribution(
        scenarios, horizon_years=horizon_years, target_moic=10.0, confidence=confidence,
        path_risk=path_risk,
    )


def _available_path_risk() -> PathRiskResult:
    return PathRiskResult(
        status="available", unavailable_reason=None,
        expected_max_drawdown=0.42, p_mdd_above_30=0.7, p_mdd_above_50=0.3, p_mdd_above_70=0.05,
        dd_excess=0.08, recovery_time_median_days=210.0, recovery_time_p90_days=520.0,
        recovery_time_unavailable_reason=None, observations_used=700, weekly_bars_used=140,
        simulations=300, fraction_drawdowns_recovered=0.9,
    )


def test_contract_version_is_racr3_on_available_and_unavailable():
    assert _base_distribution()["contract_version"] == "v5.racr3"
    assert unavailable_distribution(target_moic=10.0, confidence=0.0)["contract_version"] == "v5.racr3"


def test_path_risk_fields_populate_from_available_result():
    dist = _base_distribution(path_risk=_available_path_risk())
    assert dist["expected_max_drawdown"] == pytest.approx(0.42)
    assert dist["expected_max_drawdown_unavailable_reason"] is None
    assert dist["p_mdd_above_30"] == pytest.approx(0.7)
    assert dist["p_mdd_above_50"] == pytest.approx(0.3)
    assert dist["p_mdd_above_70"] == pytest.approx(0.05)
    assert dist["expected_drawdown_excess_35"] == pytest.approx(0.08)
    assert dist["recovery_time_median"] == pytest.approx(210.0)
    assert dist["recovery_time_p90"] == pytest.approx(520.0)
    assert dist["recovery_time_median_unavailable_reason"] is None
    assert dist["path_risk_method"] == "block_bootstrap_weekly_v1"
    assert dist["path_risk_observations_used"] == 700
    assert dist["path_risk_simulations"] == 300
    # Permanent loss is untouched by any of this.
    assert dist["p_permanent_loss"] is None
    assert dist["p_permanent_loss_unavailable_reason"] == "competing_risk_model_not_implemented"


def test_path_risk_fields_none_with_reason_when_ticker_unavailable():
    unavailable_result = PathRiskResult(
        status="unavailable", unavailable_reason="insufficient_price_history",
        expected_max_drawdown=None, p_mdd_above_30=None, p_mdd_above_50=None, p_mdd_above_70=None,
        dd_excess=None, recovery_time_median_days=None, recovery_time_p90_days=None,
        recovery_time_unavailable_reason="insufficient_price_history",
        observations_used=42, weekly_bars_used=0, simulations=0, fraction_drawdowns_recovered=None,
    )
    dist = _base_distribution(path_risk=unavailable_result)
    assert dist["expected_max_drawdown"] is None
    assert dist["expected_max_drawdown_unavailable_reason"] == "insufficient_price_history"
    assert dist["p_mdd_above_30_unavailable_reason"] == "insufficient_price_history"
    assert dist["expected_drawdown_excess_35"] is None
    assert dist["expected_drawdown_excess_35_unavailable_reason"] == "insufficient_price_history"
    assert dist["recovery_time_median"] is None


def test_path_risk_fields_none_with_not_provided_reason_when_absent():
    dist = _base_distribution(path_risk=None)
    assert dist["expected_max_drawdown"] is None
    assert dist["expected_max_drawdown_unavailable_reason"] == "path_simulation_not_provided"


def test_all_path_risk_fields_none_when_whole_distribution_unavailable():
    dist = unavailable_distribution(target_moic=10.0, confidence=0.0)
    for field in (
        "expected_max_drawdown", "p_mdd_above_30", "p_mdd_above_50", "p_mdd_above_70",
        "expected_drawdown_excess_35", "recovery_time_median", "recovery_time_p90",
        "path_risk_method", "path_risk_observations_used",
    ):
        assert dist[field] is None


# ---------------------------------------------------------------------------
# F1-2: RACR objective -- DDExcess live, permanent_loss still omitted
# ---------------------------------------------------------------------------

def _racr_config():
    from autoscreener.config import ObjectiveDefinition, ObjectivesConfig

    return ObjectivesConfig(
        default_objective="ten_bagger",
        objectives={
            "ten_bagger": ObjectiveDefinition(description="test"),
            "risk_adjusted_compounding": ObjectiveDefinition(
                description="test", tail_lambda=0.35, failure_lambda=0.20,
                drawdown_lambda=0.10, permanent_loss_lambda=0.20, uncertainty_lambda=0.50,
            ),
        },
    )


def test_racr_drawdown_penalty_matches_lambda_times_dd_excess():
    config = _racr_config()
    with_dd = evaluate_objectives(
        _base_distribution(path_risk=_available_path_risk()), config, horizon_years=7,
    )["risk_adjusted_compounding"]
    zero_dd_result = PathRiskResult(
        status="available", unavailable_reason=None,
        expected_max_drawdown=0.0, p_mdd_above_30=0.0, p_mdd_above_50=0.0, p_mdd_above_70=0.0,
        dd_excess=0.0, recovery_time_median_days=None, recovery_time_p90_days=None,
        recovery_time_unavailable_reason="insufficient_recoveries_within_horizon",
        observations_used=700, weekly_bars_used=140, simulations=300,
        fraction_drawdowns_recovered=None,
    )
    without_dd = evaluate_objectives(
        _base_distribution(path_risk=zero_dd_result), config, horizon_years=7,
    )["risk_adjusted_compounding"]
    # Same everything else (same seed distribution/confidence) -- the only
    # difference is dd_excess 0.08 vs 0.0, so the RACR gap must be exactly
    # drawdown_lambda * 0.08.
    assert without_dd.score_value - with_dd.score_value == pytest.approx(0.10 * 0.08, abs=1e-9)
    assert with_dd.explanation["omitted_terms"] == ["permanent_loss"]


def test_permanent_loss_always_omitted_and_drawdown_omitted_when_unavailable():
    config = _racr_config()
    dist = _base_distribution(path_risk=None)  # path risk not provided -> unavailable
    result = evaluate_objectives(dist, config, horizon_years=7)["risk_adjusted_compounding"]
    assert result.explanation["omitted_terms"] == ["drawdown", "permanent_loss"]
    assert result.explanation["dd_excess"] == 0.0
    assert result.explanation["p_permanent_loss"] == 0.0


def test_p_permanent_loss_stays_none_regardless_of_path_risk_availability():
    dist = _base_distribution(path_risk=_available_path_risk())
    assert dist["p_permanent_loss"] is None
    assert dist["p_permanent_loss_unavailable_reason"] == "competing_risk_model_not_implemented"


# ---------------------------------------------------------------------------
# F1-3: feature registry freshness half-lives
# ---------------------------------------------------------------------------

def test_fourteen_default_enabled_features_no_longer_all_lack_half_life():
    enabled = [f for f in FEATURE_REGISTRY if f.default_enabled]
    assert len(enabled) == 14
    with_half_life = [f for f in enabled if f.freshness_half_life_days is not None]
    without = {f.key: f.notes for f in enabled if f.freshness_half_life_days is None}
    assert len(with_half_life) == 12, without
    # The two deliberately-None entries must say why in their notes, not
    # just leave the field blank.
    for key in ("base_financial_statements", "price_history"):
        spec = next(f for f in enabled if f.key == key)
        assert spec.freshness_half_life_days is None
        assert "deliberately" in spec.notes.lower() or "WP-F1" in spec.notes


def test_financial_statement_features_share_270_day_half_life():
    by_key = {f.key: f for f in FEATURE_REGISTRY}
    for key in (
        "incremental_roic", "per_share_economics", "cash_conversion",
        "accounting_quality", "reconciliation_confidence",
    ):
        assert by_key[key].freshness_half_life_days == 270


def test_filing_derived_tail_capital_features_share_180_day_half_life():
    by_key = {f.key: f for f in FEATURE_REGISTRY}
    for key in (
        "capital_allocation", "debt_maturity", "liquidity",
        "future_dilution_capacity", "customer_concentration", "litigation",
    ):
        assert by_key[key].freshness_half_life_days == 180


def test_macro_regime_has_shortest_half_life():
    by_key = {f.key: f for f in FEATURE_REGISTRY}
    assert by_key["macro_regime"].freshness_half_life_days == 90


# ---------------------------------------------------------------------------
# End-to-end run_v5_shadow guard: expected_max_drawdown must not collapse
# ---------------------------------------------------------------------------

def _fake_compute_moic(moic_inputs, *_args, **_kwargs):
    return SimpleNamespace(
        probability=0.04, expected_moic=2.0, median_moic=1.6,
        log_moic_mu=math.log(2.0) - 0.5 * 0.6**2, log_moic_sigma=0.6,
        survival_probability=0.9, initial_growth_rate=0.10,
        terminal_growth_rate=0.04, revenue_multiple=2.0, terminal_gross_margin=0.45,
        dilution_drag=1.05, projected_net_debt=20.0, current_ev_to_gross_profit=4.0,
        multiple_change=0.9, growth_fade_rate=0.7,
    )


def test_run_v5_shadow_expected_max_drawdown_varies_with_real_price_history(monkeypatch):
    """The exact regression this WP exists to prevent: two tickers with
    genuinely different realized price history must not both score the
    same `expected_max_drawdown` -- if they do, path_risk.py has been
    re-derived from something universe-wide (the V4-seed-collinearity
    failure docs/racr_shadow_run_diagnostic_2026-09-04.md §6 describes)
    rather than from each ticker's own price_snapshots."""
    # A fresh, unique-per-invocation symbol pair (rather than a fixed
    # "ZZWPF1A"/"ZZWPF1B") -- this test creates real rows in the shared test
    # DB and other tests (e.g. apply_gates) can pick up any ticker present
    # and attach their own FK-referencing rows to it, which then blocks a
    # fixed-symbol "delete old ticker with this symbol" cleanup on rerun.
    # A unique symbol sidesteps needing that cleanup at all.
    suffix = uuid.uuid4().hex[:6].upper()
    symbols = [f"Z{suffix}A", f"Z{suffix}B"]
    ticker_ids: list[int] = []
    with session_scope() as session:
        for symbol in symbols:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            ticker_ids.append(ticker.id)

    start = datetime.date(2016, 1, 1)
    calm_obs = tuple(_observations(_flat_series(700, price=80.0), start=start))
    turbulent_obs = tuple(_observations(_crash_cycle_series(700), start=start))

    items = [
        V5PitInput(
            ticker_id=ticker_ids[0], symbol=symbols[0], as_of=AS_OF,
            moic_inputs=SimpleNamespace(variant=0, fcf_margin=0.08, net_debt=100.0),
            raw_snapshot_id=1, raw_available_from=AS_OF, price_as_of=AS_OF,
            input_status="collected_with_data", price_row_count=len(calm_obs),
            price_observations=calm_obs,
        ),
        V5PitInput(
            ticker_id=ticker_ids[1], symbol=symbols[1], as_of=AS_OF,
            moic_inputs=SimpleNamespace(variant=1, fcf_margin=0.08, net_debt=100.0),
            raw_snapshot_id=2, raw_available_from=AS_OF, price_as_of=AS_OF,
            input_status="collected_with_data", price_row_count=len(turbulent_obs),
            price_observations=turbulent_obs,
        ),
    ]

    monkeypatch.setattr("autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: items)
    monkeypatch.setattr("autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object())
    monkeypatch.setattr("autoscreener.scoring.v5.engine.compute_moic", _fake_compute_moic)

    run_id = None
    try:
        result = run_v5_shadow(AS_OF)
        run_id = result["run_id"]
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            scores = {
                score.ticker_id: score.distribution
                for score in session.query(ModelScore).filter_by(run_id=run_id).all()
            }
            mdd_calm = scores[ticker_ids[0]]["expected_max_drawdown"]
            mdd_turbulent = scores[ticker_ids[1]]["expected_max_drawdown"]
            assert mdd_calm is not None and mdd_turbulent is not None
            assert mdd_turbulent - mdd_calm > 0.15, (
                f"expected_max_drawdown collapsed across tickers with different "
                f"realized price history: calm={mdd_calm}, turbulent={mdd_turbulent}"
            )
            assert "expected_max_drawdown" not in (
                run.metrics["objective_diagnostics"]["distribution_constant_fields"]
            )
            diag = run.metrics["path_risk_diagnostics"]
            assert diag["expected_max_drawdown"]["distinct"] == 2
            assert diag["available_count"] == 2
    finally:
        # This test writes real rows to the shared test DB (ModelRun/
        # ModelScore/ObjectiveScore/ModelFeatureValue/Ticker). An earlier
        # version of this test left them behind, and a *later* full-suite
        # run picked up the leftover Ticker rows as extra "no_data"
        # tickers in test_apply_gates_concurrent_deletion's exact-count
        # assertion. Clean up unconditionally (even on assertion failure)
        # so this test is self-contained and cannot pollute a later run.
        with session_scope() as session:
            if run_id is not None:
                session.query(ObjectiveScore).filter_by(run_id=run_id).delete(synchronize_session=False)
                session.query(ModelFeatureValue).filter_by(run_id=run_id).delete(synchronize_session=False)
                session.query(ModelScore).filter_by(run_id=run_id).delete(synchronize_session=False)
                session.query(ModelRun).filter_by(id=run_id).delete(synchronize_session=False)
            session.query(ObjectiveScore).filter(
                ObjectiveScore.ticker_id.in_(ticker_ids)
            ).delete(synchronize_session=False)
            session.query(ModelFeatureValue).filter(
                ModelFeatureValue.ticker_id.in_(ticker_ids)
            ).delete(synchronize_session=False)
            session.query(ModelScore).filter(
                ModelScore.ticker_id.in_(ticker_ids)
            ).delete(synchronize_session=False)
            session.query(Ticker).filter(Ticker.id.in_(ticker_ids)).delete(synchronize_session=False)
