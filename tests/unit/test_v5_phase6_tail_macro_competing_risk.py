from __future__ import annotations

import datetime
import math
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import (
    CustomerConcentration,
    DilutionCapacity,
    LitigationEvent,
    MacroExposureSnapshot,
    ModelRun,
    ModelScore,
    Score,
    Ticker,
)
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.balance_sheet import (
    CapitalFeatureSet,
    CapitalSignal,
    apply_capital_features,
    build_capital_feature_sets,
)
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.growth import GrowthFeatureSet
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.quality import QualityFeatureSet, QualityUpdate
from autoscreener.scoring.v5.scenario import build_scenarios
from autoscreener.scoring.v5.tail_risk import (
    TailFeatureSet,
    TailSignal,
    apply_tail_features,
    build_tail_feature_sets,
)


def _capital_signal(key, value, *, applied=True, evidence=None, coverage=CoverageStatus.COLLECTED_WITH_DATA):
    return CapitalSignal(
        key=key, status="applied" if applied else str(coverage), coverage_status=coverage,
        runtime_enabled=True, applied=applied, reliability=0.60, observed_at=None, value=value,
        evidence=evidence or {},
    )


def _capital_features(*signals):
    return CapitalFeatureSet(tuple(signals), {s.key: 1.0 for s in signals})


def _tail_signal(key, value, *, applied=True, evidence=None, coverage=CoverageStatus.COLLECTED_WITH_DATA):
    return TailSignal(
        key=key, status="applied" if applied else str(coverage), coverage_status=coverage,
        runtime_enabled=True, applied=applied, reliability=0.60, observed_at=None, value=value,
        evidence=evidence or {},
    )


def _tail_features(*signals):
    return TailFeatureSet(tuple(signals), {s.key: 1.0 for s in signals})


def _seed_result(survival: float = 0.94):
    return SimpleNamespace(
        log_moic_mu=math.log(2.0) - 0.5 * 0.7**2, log_moic_sigma=0.7, survival_probability=survival,
    )


def _seed_ticker(session, symbol: str) -> int:
    """WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):これらの
    テストは自分では作らない「DBに既に1件Tickerがある」前提だったが、隔離
    済みテストDBは0件から始まる。FK対象のTickerを同一トランザクション内で
    作り(呼び出し側が最後に `session.rollback()` するため恒久書き込みには
    ならない)、そのidを返す。"""
    ticker = Ticker(symbol=symbol, market="US")
    session.add(ticker)
    session.flush()
    return ticker.id


# ---------------------------------------------------------------------------
# M&A competing risk: deliberately not implemented.
# ---------------------------------------------------------------------------

def test_acquisition_competing_risk_stays_disabled_and_unimplemented():
    config = load_model_v5_config()
    assert config.feature_flags["acquisition_competing_risk"] is False
    spec = FEATURES_BY_KEY["acquisition_competing_risk"]
    assert spec.default_enabled is False
    # No builder module exposes a build_*/apply_* pair for this key -- the
    # registry entry documents why (94/94 unknown), it does not gate a
    # signal that was actually implemented.
    import autoscreener.scoring.v5.tail_risk as tail_risk_module
    assert not hasattr(tail_risk_module, "_acquisition_signal")


# ---------------------------------------------------------------------------
# customer_concentration
# ---------------------------------------------------------------------------

def test_customer_concentration_caps_total_disclosure_at_one():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT1")
        for i, pct in enumerate((0.60, 0.55)):  # sums to 1.15 -- must clip
            session.add(CustomerConcentration(
                ticker_id=ticker_id, period_end=datetime.date(2023, 12, 31),
                customer_label=f"Customer {i}", revenue_pct=pct, source="text",
                collected_on=as_of,
            ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT1", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "customer_concentration")
    assert signal.value == pytest.approx(1.0)


def test_customer_concentration_widens_left_tail_only():
    config = load_model_v5_config()
    signal = _tail_signal("customer_concentration", 0.60)
    update = apply_tail_features(_tail_features(signal), config=config)
    assert update.left_tail_extra > 0.0
    assert update.left_tail_extra <= config.tail.customer_concentration_left_tail_max


# ---------------------------------------------------------------------------
# litigation: no severity/amount field exists -- event-count proxy.
# ---------------------------------------------------------------------------

def test_litigation_no_recent_events_is_no_change_not_missing():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT2")
        session.add(LitigationEvent(
            ticker_id=ticker_id, event_date=datetime.date(2020, 1, 1), kind="securities_class_action",
            title="Old case", collected_on=as_of,
        ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT2", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "litigation")
    assert signal.coverage_status == CoverageStatus.COLLECTED_WITH_DATA
    assert signal.value == pytest.approx(0.0)


def test_litigation_recent_event_count_is_bounded_severity():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT3")
        for i in range(5):  # well above severity_count_cap=3 -- must clip to 1.0
            session.add(LitigationEvent(
                ticker_id=ticker_id, event_date=datetime.date(2024, i + 1, 1),
                kind="sec_investigation", title=f"Case {i}", collected_on=as_of,
            ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT3", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "litigation")
    assert signal.value == pytest.approx(1.0)


def test_litigation_ignores_events_filed_after_as_of():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT4")
        session.add(LitigationEvent(
            ticker_id=ticker_id, event_date=datetime.date(2024, 3, 1), kind="short_report",
            title="Visible case", collected_on=as_of,
        ))
        session.add(LitigationEvent(
            ticker_id=ticker_id, event_date=datetime.date(2024, 8, 1), kind="short_report",
            title="Future case", collected_on=datetime.date(2024, 8, 1),
        ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT4", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "litigation")
    assert signal.evidence["event_count_in_window"] == 1


# ---------------------------------------------------------------------------
# macro_regime: fred_vintage_supported gate; downside_beta only, never a
# bonus for defensive (negative) betas.
# ---------------------------------------------------------------------------

def test_macro_regime_rejects_fred_vintage_unsupported_as_not_applicable():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT5")
        session.add(MacroExposureSnapshot(
            ticker_id=ticker_id, observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            observation_end=datetime.date(2024, 5, 31), factor="DGS10", beta=1.2, downside_beta=1.8,
            sample_count=60, source="test", coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
            confidence="medium", raw_payload={"fred_vintage_supported": False},
        ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT5", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "macro_regime")
    assert signal.coverage_status == CoverageStatus.NOT_APPLICABLE
    assert signal.value is None


def test_macro_regime_negative_downside_beta_gets_no_bonus():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT6")
        session.add(MacroExposureSnapshot(
            ticker_id=ticker_id, observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            observation_end=datetime.date(2024, 5, 31), factor="DGS10", beta=-0.5, downside_beta=-0.8,
            sample_count=60, source="test", coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
            confidence="medium", raw_payload={"fred_vintage_supported": True},
        ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT6", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_tail_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "macro_regime")
    assert signal.value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# apply_tail_features: additive composition, shared cap, empty is a no-op.
# ---------------------------------------------------------------------------

def test_apply_tail_features_empty_is_noop():
    config = load_model_v5_config()
    update = apply_tail_features(_tail_features(), config=config)
    assert update.left_tail_extra == pytest.approx(0.0)
    assert update.applied_keys == ()


def test_apply_tail_features_composes_additively_and_caps():
    config = load_model_v5_config()
    signals = _tail_features(
        _tail_signal("customer_concentration", 1.0),
        _tail_signal("litigation", 1.0),
        _tail_signal("macro_regime", 5.0),
    )
    update = apply_tail_features(signals, config=config)
    uncapped = (
        config.tail.customer_concentration_left_tail_max
        + config.tail.litigation_left_tail_max + config.tail.macro_regime_left_tail_max
    )
    assert uncapped > config.tail.max_combined_left_tail_extra  # the cap must actually bind
    assert update.left_tail_extra == pytest.approx(config.tail.max_combined_left_tail_extra)


# ---------------------------------------------------------------------------
# future_dilution_capacity: mean-multiplier decay with an explicit
# anti-triple-counting budget shared with Phase 4 per_share_economics.
# ---------------------------------------------------------------------------

def test_future_dilution_capacity_signal_uses_market_cap_and_options_ratio():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker_id = _seed_ticker(session, "ZZT7")
        session.add(DilutionCapacity(
            ticker_id=ticker_id, as_of_date=as_of, collected_on=as_of,
            atm_remaining_usd=50_000_000.0, shelf_remaining_usd=None,
            unexercised_options_ratio=0.05, has_variable_conversion=False,
        ))
        session.flush()
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZT7", as_of=as_of,
            moic_inputs=SimpleNamespace(market_cap=500_000_000.0),
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        feature_sets = build_capital_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "future_dilution_capacity")
    # equity_capacity_ratio = 50M / 500M = 0.10; + options_ratio 0.05 = 0.15
    assert signal.value == pytest.approx(0.15)


def test_future_dilution_capacity_reduces_mean_multiplier_when_budget_available():
    config = load_model_v5_config()
    signal = _capital_signal("future_dilution_capacity", 0.30, evidence={})
    update = apply_capital_features(_seed_result(), _capital_features(signal), config=config)
    assert update.mean_multiplier < 1.0
    assert update.applied_keys == ("future_dilution_capacity",)
    assert update.no_effect_keys == ()


def test_future_dilution_capacity_no_effect_when_shared_budget_exhausted():
    """Anti-triple-counting: if Phase 4's per_share_economics/incremental_roic
    already spent the entire max_combined_dilution_reduction budget, this
    signal must not add further reduction on top -- it becomes a genuine
    no-op, not a fabricated stacked penalty (user-decided 2026-09-03)."""
    config = load_model_v5_config()
    exhausted_quality_update = QualityUpdate(
        duration_multiplier=1.0,
        mean_multiplier=1.0 - config.capital.max_combined_dilution_reduction,
        sigma_multiplier=1.0, left_tail_extra=0.0, confidence_penalty=0.0,
        applied_keys=("per_share_economics",), signal_effects={},
    )
    signal = _capital_signal("future_dilution_capacity", 0.30, evidence={})
    update = apply_capital_features(
        _seed_result(), _capital_features(signal), config=config,
        quality_update=exhausted_quality_update,
    )
    assert update.mean_multiplier == pytest.approx(1.0)
    assert update.applied_keys == ()
    assert update.no_effect_keys == ("future_dilution_capacity",)


def test_future_dilution_capacity_reduction_shrinks_with_already_used_budget():
    config = load_model_v5_config()
    # A large overhang whose raw reduction (weight * overhang) would hit
    # future_dilution_max_reduction on its own if the shared budget were not
    # the binding constraint -- chosen so this test actually exercises the
    # budget path rather than the signal's own per-signal cap.
    overhang = config.capital.future_dilution_max_reduction / config.capital.future_dilution_weight
    signal = _capital_signal("future_dilution_capacity", overhang, evidence={})
    mostly_used_quality_update = QualityUpdate(
        duration_multiplier=1.0,
        # Leaves only a small sliver of the shared budget remaining --
        # smaller than future_dilution_max_reduction, so the shared budget
        # (not the per-signal cap) is what binds here.
        mean_multiplier=1.0 - (config.capital.max_combined_dilution_reduction - 0.05),
        sigma_multiplier=1.0, left_tail_extra=0.0, confidence_penalty=0.0,
        applied_keys=("per_share_economics",), signal_effects={},
    )
    full_budget = apply_capital_features(
        _seed_result(), _capital_features(signal), config=config, quality_update=None,
    )
    mostly_used_budget = apply_capital_features(
        _seed_result(), _capital_features(signal), config=config,
        quality_update=mostly_used_quality_update,
    )
    assert mostly_used_budget.mean_multiplier > full_budget.mean_multiplier


# ---------------------------------------------------------------------------
# Regression: empty Phase 6 features reproduce Phase 2-5 output exactly.
# ---------------------------------------------------------------------------

def test_default_tail_and_dilution_config_reproduces_prior_phase_distribution_exactly():
    config = load_model_v5_config()
    result = _seed_result()
    baseline = build_scenarios(result, confidence=0.5, config=config)
    empty_tail_update = apply_tail_features(_tail_features(), config=config)
    empty_capital_update = apply_capital_features(result, _capital_features(), config=config)
    no_op = build_scenarios(
        result, confidence=0.5, config=config,
        conditional_mean_multiplier=empty_capital_update.mean_multiplier,
        left_tail_extra=empty_tail_update.left_tail_extra,
        survival_multiplier=empty_capital_update.survival_multiplier,
    )
    for base_s, noop_s in zip(baseline, no_op):
        assert noop_s.log_mu == pytest.approx(base_s.log_mu)
        assert noop_s.log_sigma == pytest.approx(base_s.log_sigma)
        assert noop_s.survival_probability == pytest.approx(base_s.survival_probability)


# ---------------------------------------------------------------------------
# End-to-end engine wiring.
# ---------------------------------------------------------------------------

def test_shadow_run_persists_tail_ablation_without_touching_v4(monkeypatch):
    # WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):以前は
    # 「DBに既にTickerが1件ある」前提で `.first()` を読んでいたが、隔離済み
    # テストDBは0件から始まるため `None` を返し `.id` で落ちていた。V5 shadow
    # runの永続化を確認するだけなので、対象Tickerは自前で1件作れば十分。
    as_of = datetime.date(2024, 6, 30)
    symbol = "ZZV5TAILSHADOW"
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.delete(old)
            session.flush()
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        v4_before = session.query(Score).count()
    item = V5PitInput(
        ticker_id=ticker_id, symbol=symbol, as_of=as_of,
        moic_inputs=SimpleNamespace(fcf_margin=0.08, net_debt=100.0, market_cap=500_000_000.0),
        raw_snapshot_id=1, raw_available_from=as_of,
        price_as_of=as_of, input_status="collected_with_data",
    )
    tail_set = _tail_features(_tail_signal("customer_concentration", 0.60, evidence={}))
    empty_growth = GrowthFeatureSet((), {})
    empty_quality = QualityFeatureSet((), {})
    empty_capital = CapitalFeatureSet((), {})
    result = SimpleNamespace(
        log_moic_mu=0.64, log_moic_sigma=0.75, survival_probability=0.91,
        initial_growth_rate=0.12, terminal_growth_rate=0.04,
        growth_fade_rate=0.75, revenue_multiple=2.0,
        terminal_gross_margin=0.45, dilution_drag=1.1,
        projected_net_debt=50.0, current_ev_to_gross_profit=4.0,
        multiple_change=0.8,
    )
    monkeypatch.setattr("autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: [item])
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_growth_feature_sets", lambda *a, **k: {ticker_id: empty_growth}
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_quality_feature_sets", lambda *a, **k: {ticker_id: empty_quality}
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_capital_feature_sets", lambda *a, **k: {ticker_id: empty_capital}
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_tail_feature_sets", lambda *a, **k: {ticker_id: tail_set}
    )
    monkeypatch.setattr("autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object())
    monkeypatch.setattr("autoscreener.scoring.v5.engine.compute_moic", lambda *a, **k: result)
    output = run_v5_shadow(as_of)
    run_id = output["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            score = session.query(ModelScore).filter_by(run_id=run_id).one()
            impact = score.features["ablation"]["customer_concentration"]
            assert score.states["contract_version"] == "v5.phase6"
            assert "customer_concentration" in score.states["state_updates_applied"]
            assert impact["status"] == "computed"
            assert impact["state_shift"]["left_tail_extra"] > 0
            assert impact["scenario_impact"]["p_moic_below_1_0"] > 0
            assert run.metrics["applied_feature_counts"] == {"customer_concentration": 1}
            assert run.metrics["code_revision"]["commit"] is not None or run.metrics["code_revision"]["reason"]
            assert session.query(Score).count() == v4_before
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()
