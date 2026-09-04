from __future__ import annotations

import datetime
import math
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import (
    CapitalAllocationEvent,
    DebtInstrument,
    LiquidityFacility,
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
from autoscreener.scoring.v5.growth import GrowthFeatureSet
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.quality import QualityFeatureSet
from autoscreener.scoring.v5.scenario import build_scenarios


def _signal(
    key: str, value: float | None, *, applied: bool = True,
    reliability: float = 0.65, evidence: dict | None = None,
    coverage: str = CoverageStatus.COLLECTED_WITH_DATA,
) -> CapitalSignal:
    return CapitalSignal(
        key=key, status="applied" if applied else str(coverage),
        coverage_status=coverage, runtime_enabled=True, applied=applied,
        reliability=reliability, observed_at=None, value=value,
        evidence=evidence or {},
    )


def _features(*signals: CapitalSignal) -> CapitalFeatureSet:
    return CapitalFeatureSet(tuple(signals), {signal.key: 1.0 for signal in signals})


def _seed_result(survival: float = 0.94):
    return SimpleNamespace(
        log_moic_mu=math.log(2.0) - 0.5 * 0.7**2, log_moic_sigma=0.7,
        survival_probability=survival,
    )


# ---------------------------------------------------------------------------
# apply_capital_features: survival_multiplier is shrink-only, composes
# multiplicatively, and is a no-op for an empty feature set.
# ---------------------------------------------------------------------------

def test_empty_feature_set_reproduces_baseline_exactly():
    config = load_model_v5_config()
    update = apply_capital_features(SimpleNamespace(), _features(), config=config)
    assert update.survival_multiplier == pytest.approx(1.0)
    assert update.applied_keys == ()


def test_debt_maturity_shortfall_shrinks_survival_never_grants_bonus():
    config = load_model_v5_config()
    stressed = _signal("debt_maturity", 0.50)  # 50% short of full coverage
    update = apply_capital_features(SimpleNamespace(), _features(stressed), config=config)
    assert update.survival_multiplier < 1.0
    assert update.survival_multiplier >= config.capital.debt_maturity_min_survival_multiplier

    covered = _signal("debt_maturity", 0.0)
    baseline = apply_capital_features(SimpleNamespace(), _features(covered), config=config)
    assert baseline.survival_multiplier == pytest.approx(1.0)


def test_multiple_applied_signals_compose_multiplicatively():
    config = load_model_v5_config()
    debt = _signal("debt_maturity", 0.30)
    liquidity = _signal("liquidity", 6.0)
    combined = apply_capital_features(SimpleNamespace(), _features(debt, liquidity), config=config)
    debt_only = apply_capital_features(SimpleNamespace(), _features(debt), config=config)
    liquidity_only = apply_capital_features(SimpleNamespace(), _features(liquidity), config=config)
    assert combined.survival_multiplier == pytest.approx(
        debt_only.survival_multiplier * liquidity_only.survival_multiplier
    )


def test_net_capital_raiser_gets_no_penalty():
    """A company raising more than it returns must not be treated as risky
    -- only a committed net cash *return* large relative to cash is a
    liquidity-stress signal (see _capital_allocation_signal)."""
    config = load_model_v5_config()
    raiser = _signal("capital_allocation", 0.0)  # net_commitment <= 0 -> shortfall 0
    update = apply_capital_features(SimpleNamespace(), _features(raiser), config=config)
    assert update.survival_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Signal builders: missingness is neutral, coverage gate disables even a
# ticker with a usable row, PIT boundary is respected.
# ---------------------------------------------------------------------------

def test_missing_liquidity_facility_leaves_debt_and_liquidity_not_collected():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        # Real, existing ticker id required: DebtInstrument.ticker_id is a
        # foreign key into tickers, so an arbitrary literal id would either
        # violate the FK or (worse) coincidentally collide with unrelated
        # real data. WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):
        # the isolated test DB starts with zero tickers, so create one inside
        # this same transaction (visible to the FK check via flush ordering)
        # instead of assuming one already exists. Rolled back below -- no
        # permanent write.
        ticker = Ticker(symbol="ZZB1", market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        item = V5PitInput(
            ticker_id=ticker_id, symbol="ZZB1", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        session.add(DebtInstrument(
            ticker_id=ticker_id, instrument_id="test-only-d1", as_of=as_of,
            observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            principal=100.0, maturity_date=datetime.date(2024, 12, 1), source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
        session.flush()
        feature_sets = build_capital_feature_sets(session, [item], as_of=as_of, config=config)
        session.rollback()
    debt_signal = next(s for s in feature_sets[ticker_id].signals if s.key == "debt_maturity")
    liquidity_signal = next(s for s in feature_sets[ticker_id].signals if s.key == "liquidity")
    assert debt_signal.value is None
    assert liquidity_signal.value is None


def test_low_coverage_runtime_gate_disables_feature_even_with_a_row():
    config = load_model_v5_config()
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        # WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md): same
        # reasoning as test_missing_liquidity_facility_leaves_debt_and_liquidity_not_collected
        # above -- create the ticker in this transaction rather than assume
        # one already exists; rolled back below, no permanent write.
        ticker = Ticker(symbol="ZZB7", market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        has_data = V5PitInput(
            ticker_id=ticker_id, symbol="ZZB7", as_of=as_of, moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="collected_with_data", financial_annual=(),
        )
        # No DB rows are ever written for these fabricated ids -- they only
        # need to exist in the in-memory items list to dilute universe
        # coverage, so no FK constraint applies to them.
        no_data = [
            V5PitInput(
                ticker_id=-(200 + i), symbol=f"ZZBN{i}", as_of=as_of,
                moic_inputs=None, raw_snapshot_id=None, raw_available_from=None,
                price_as_of=None, input_status="not_collected", financial_annual=(),
            )
            for i in range(20)
        ]
        session.add(LiquidityFacility(
            ticker_id=ticker_id, as_of=as_of,
            observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            cash_balance=1000.0, revolver_available=500.0, source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
        session.add(DebtInstrument(
            ticker_id=ticker_id, instrument_id="test-only-d1", as_of=as_of,
            observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            principal=2000.0, maturity_date=datetime.date(2024, 12, 1), source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
        session.flush()
        feature_sets = build_capital_feature_sets(
            session, [has_data, *no_data], as_of=as_of, config=config
        )
        session.rollback()
    signal = next(s for s in feature_sets[ticker_id].signals if s.key == "debt_maturity")
    assert signal.coverage_status == CoverageStatus.COLLECTED_WITH_DATA
    assert signal.status == "runtime_disabled_low_coverage"
    assert signal.applied is False
    assert feature_sets[ticker_id].universe_coverage["debt_maturity"] < 0.50


def test_capital_allocation_ignores_events_filed_after_as_of():
    config = load_model_v5_config()
    symbol = "ZZV5CAPPIT"
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.query(CapitalAllocationEvent).filter_by(ticker_id=old.id).delete()
            session.query(LiquidityFacility).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(LiquidityFacility(
            ticker_id=ticker_id, as_of=as_of,
            observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
            cash_balance=100.0, source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
        # Visible: a large buyback announced well within the window.
        session.add(CapitalAllocationEvent(
            ticker_id=ticker_id, announced_at=datetime.datetime(2024, 4, 1, tzinfo=datetime.timezone.utc),
            observed_at=datetime.datetime(2024, 4, 1, tzinfo=datetime.timezone.utc),
            event_type="buyback", amount=80.0, source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
        # Invisible: announced after as_of -- must never be read.
        session.add(CapitalAllocationEvent(
            ticker_id=ticker_id, announced_at=datetime.datetime(2024, 8, 1, tzinfo=datetime.timezone.utc),
            observed_at=datetime.datetime(2024, 8, 1, tzinfo=datetime.timezone.utc),
            event_type="buyback", amount=1_000_000.0, source="test",
            coverage_status=CoverageStatus.COLLECTED_WITH_DATA, confidence="medium",
        ))
    item = V5PitInput(
        ticker_id=ticker_id, symbol=symbol, as_of=as_of, moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(),
    )
    try:
        with session_scope() as session:
            feature_sets = build_capital_feature_sets(session, [item], as_of=as_of, config=config)
        signal = next(s for s in feature_sets[ticker_id].signals if s.key == "capital_allocation")
        assert signal.evidence["event_count_in_window"] == 1
        assert signal.evidence["outflow_buyback_dividend"] == pytest.approx(80.0)
    finally:
        with session_scope() as session:
            session.query(CapitalAllocationEvent).filter_by(ticker_id=ticker_id).delete()
            session.query(LiquidityFacility).filter_by(ticker_id=ticker_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()


# ---------------------------------------------------------------------------
# survival_multiplier actually reaches the distribution: p_moic_below_1_0
# and expected_moic move, not just a diagnostic number.
# ---------------------------------------------------------------------------

def test_survival_multiplier_moves_the_distribution():
    config = load_model_v5_config()
    result = _seed_result(survival=0.94)
    baseline = build_scenarios(result, confidence=0.5, config=config)
    stressed = build_scenarios(result, confidence=0.5, config=config, survival_multiplier=0.85)
    assert stressed[0].survival_probability == pytest.approx(0.94 * 0.85)
    assert stressed[0].survival_probability < baseline[0].survival_probability
    with pytest.raises(ValueError):
        build_scenarios(result, confidence=0.5, config=config, survival_multiplier=1.01)


def test_default_capital_config_reproduces_phase2_phase4_distribution_exactly():
    config = load_model_v5_config()
    result = _seed_result()
    baseline = build_scenarios(result, confidence=0.5, config=config)
    no_op = build_scenarios(result, confidence=0.5, config=config, survival_multiplier=1.0)
    for base_s, noop_s in zip(baseline, no_op):
        assert noop_s.survival_probability == pytest.approx(base_s.survival_probability)


# ---------------------------------------------------------------------------
# End-to-end engine wiring: applied capital signals move survival in the
# persisted state, carry a computed ablation, and never touch v4.
# ---------------------------------------------------------------------------

def test_shadow_run_persists_capital_ablation_without_touching_v4(monkeypatch):
    # WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):以前は
    # 「DBに既にTickerが1件ある」前提で `.first()` を読んでいたが、隔離済み
    # テストDBは0件から始まるため `None` を返し `.id` で落ちていた。V5 shadow
    # runの永続化を確認するだけなので、対象Tickerは自前で1件作れば十分。
    as_of = datetime.date(2024, 6, 30)
    symbol = "ZZV5CAPSHADOW"
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
        moic_inputs=SimpleNamespace(fcf_margin=0.08, net_debt=100.0),
        raw_snapshot_id=1, raw_available_from=as_of,
        price_as_of=as_of, input_status="collected_with_data",
    )
    capital_set = _features(_signal("debt_maturity", 0.5, evidence={}))
    empty_growth = GrowthFeatureSet((), {})
    empty_quality = QualityFeatureSet((), {})
    result = SimpleNamespace(
        log_moic_mu=0.64, log_moic_sigma=0.75, survival_probability=0.91,
        initial_growth_rate=0.12, terminal_growth_rate=0.04,
        growth_fade_rate=0.75, revenue_multiple=2.0,
        terminal_gross_margin=0.45, dilution_drag=1.1,
        projected_net_debt=50.0, current_ev_to_gross_profit=4.0,
        multiple_change=0.8,
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: [item]
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_growth_feature_sets",
        lambda *a, **k: {ticker_id: empty_growth},
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_quality_feature_sets",
        lambda *a, **k: {ticker_id: empty_quality},
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.build_capital_feature_sets",
        lambda *a, **k: {ticker_id: capital_set},
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        "autoscreener.scoring.v5.engine.compute_moic", lambda *a, **k: result
    )
    output = run_v5_shadow(as_of)
    run_id = output["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            score = session.query(ModelScore).filter_by(run_id=run_id).one()
            impact = score.features["ablation"]["debt_maturity"]
            assert score.states["contract_version"] == "v5.phase6"
            assert "debt_maturity" in score.states["state_updates_applied"]
            assert score.states["competing_risk"]["survival_probability"]["status"] == "updated"
            assert score.states["competing_risk"]["survival_probability"]["value"] < 0.91
            assert impact["status"] == "computed"
            assert impact["state_shift"]["survival_multiplier"] < 0
            assert impact["scenario_impact"]["p_moic_below_1_0"] > 0
            # code_revision is recorded on every run, even a monkeypatched one.
            assert "commit" in run.metrics["code_revision"]
            assert session.query(Score).count() == v4_before
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()
