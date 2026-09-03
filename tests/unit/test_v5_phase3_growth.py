from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import AnalystConsensusSnapshot, ModelRun, ModelScore, Score, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.growth import (
    GrowthFeatureSet,
    GrowthSignal,
    apply_growth_features,
    build_growth_feature_sets,
    _guidance_signal,
    _kpi_signal,
    _tam_signal,
)
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.engine import run_v5_shadow


def _result(initial: float = 0.20):
    return SimpleNamespace(
        initial_growth_rate=initial,
        terminal_growth_rate=0.04,
        growth_fade_rate=0.75,
    )


def _signal(
    key: str, value: float | None, *, applied: bool = True,
    reliability: float = 0.65, evidence: dict | None = None,
    coverage: str = CoverageStatus.COLLECTED_WITH_DATA,
) -> GrowthSignal:
    return GrowthSignal(
        key=key, status="applied" if applied else str(coverage),
        coverage_status=coverage, runtime_enabled=True, applied=applied,
        reliability=reliability, observed_at=None, value=value,
        evidence=evidence or {},
    )


def _features(*signals: GrowthSignal) -> GrowthFeatureSet:
    return GrowthFeatureSet(tuple(signals), {signal.key: 1.0 for signal in signals})


def test_consensus_revision_updates_growth_with_bounded_positive_sign_and_ablation():
    config = load_model_v5_config()
    features = _features(_signal(
        "consensus_revision", 0.10, evidence={"years_to_period": 1.0}
    ))
    full = apply_growth_features(_result(), features, config=config)
    without = apply_growth_features(
        _result(), features, config=config, excluded_key="consensus_revision"
    )
    assert full.updated_initial_rate > without.updated_initial_rate
    assert full.revenue_multiple_ratio > 1.0
    assert without.revenue_multiple_ratio == pytest.approx(1.0)
    assert full.updated_initial_rate - 0.20 <= config.growth.max_initial_growth_adjustment


def test_negative_expectation_update_has_negative_distribution_direction():
    config = load_model_v5_config()
    features = _features(
        _signal("consensus_revision", -0.15, evidence={"years_to_period": 1.0}),
        _signal("guidance", 0.05),
    )
    update = apply_growth_features(_result(), features, config=config)
    assert update.updated_initial_rate < 0.20
    assert update.revenue_multiple_ratio < 1.0


def test_tam_is_a_duration_constraint_not_a_large_tam_bonus():
    config = load_model_v5_config()
    baseline = apply_growth_features(_result(), _features(), config=config)
    huge = apply_growth_features(
        _result(), _features(_signal("tam_headroom", 1000.0)), config=config
    )
    tight = apply_growth_features(
        _result(), _features(_signal("tam_headroom", 1.05)), config=config
    )
    assert huge.revenue_multiple_ratio == pytest.approx(baseline.revenue_multiple_ratio)
    assert huge.updated_duration_years == pytest.approx(baseline.updated_duration_years)
    assert tight.updated_duration_years < baseline.updated_duration_years
    assert tight.revenue_multiple_ratio < 1.0


def test_missingness_is_neutral_to_state_but_affects_confidence_separately():
    missing = _signal(
        "consensus_revision", None, applied=False,
        coverage=CoverageStatus.NOT_COLLECTED,
    )
    failed = _signal(
        "guidance", None, applied=False,
        coverage=CoverageStatus.COLLECTION_FAILED,
    )
    features = _features(missing, failed)
    update = apply_growth_features(_result(), features, config=load_model_v5_config())
    assert update.updated_initial_rate == pytest.approx(update.baseline_initial_rate)
    assert update.revenue_multiple_ratio == pytest.approx(1.0)
    assert features.confidence_delta < 0


def test_low_coverage_runtime_gate_prevents_coverage_advantage():
    signal = GrowthSignal(
        key="tam_headroom", status="runtime_disabled_low_coverage",
        coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
        runtime_enabled=False, applied=False, reliability=0.95,
        observed_at=None, value=1.05,
        evidence={"universe_coverage": 0.01, "required_coverage": 0.50},
    )
    features = _features(signal)
    update = apply_growth_features(_result(), features, config=load_model_v5_config())
    assert update.applied_keys == ()
    assert update.revenue_multiple_ratio == pytest.approx(1.0)
    assert features.confidence_delta == 0.0


def test_kpi_nowcast_requires_comparable_company_metric_history():
    config = load_model_v5_config()
    definition = SimpleNamespace(id=1, code="store_count", model_family="consumer")
    first = SimpleNamespace(
        id=1, ticker_id=1, period_end=datetime.date(2024, 3, 31),
        reported_at=datetime.datetime(2024, 4, 15, tzinfo=datetime.timezone.utc),
        value=100.0, confidence="medium",
        coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
    )
    latest = SimpleNamespace(
        id=2, ticker_id=1, period_end=datetime.date(2024, 6, 30),
        reported_at=datetime.datetime(2024, 7, 15, tzinfo=datetime.timezone.utc),
        value=110.0, confidence="medium",
        coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
    )
    signal = _kpi_signal([(first, definition), (latest, definition)], config)
    assert signal.status == "candidate"
    assert signal.value > 0
    assert signal.evidence["observations"][0]["code"] == "store_count"
    too_close = SimpleNamespace(**{**vars(latest), "period_end": datetime.date(2024, 4, 15)})
    rejected = _kpi_signal([(first, definition), (too_close, definition)], config)
    assert rejected.status == "insufficient_comparable_history"


def test_tam_requires_same_currency_addressable_revenue_and_does_not_trust_low_confidence():
    missing = SimpleNamespace(
        id=1, coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
        confidence="low", tam_value=1_000_000_000.0,
        current_revenue_addressable=None, currency="USD",
        observed_at=datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
        as_of=datetime.date(2024, 5, 31),
    )
    assert _tam_signal([missing]).status == "missing_required_fields"
    complete = SimpleNamespace(
        **{**vars(missing), "current_revenue_addressable": 100_000_000.0}
    )
    signal = _tam_signal([complete])
    assert signal.value == pytest.approx(10.0)
    assert signal.reliability < 0.6


def test_guidance_rejects_inverted_or_unscaled_ranges():
    as_of = datetime.date(2024, 6, 30)
    item = SimpleNamespace(moic_inputs=SimpleNamespace(revenue_latest=1_000_000_000.0))
    base = {
        "id": 1, "coverage_status": CoverageStatus.COLLECTED_WITH_DATA,
        "metric": "revenue", "period_end": datetime.date(2025, 6, 30),
        "unit": "USD", "confidence": "medium",
        "announced_at": datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc),
    }
    inverted = SimpleNamespace(**base, low=500.0, high=4.0)
    assert _guidance_signal([inverted], item, as_of).status == "invalid_guidance_range"
    unscaled = SimpleNamespace(**base, low=4800.0, high=5000.0)
    assert _guidance_signal([unscaled], item, as_of).status == "unit_or_scale_mismatch"
    valid = SimpleNamespace(**base, low=1_100_000_000.0, high=1_200_000_000.0)
    assert _guidance_signal([valid], item, as_of).status == "candidate"


def test_consensus_builder_is_point_in_time_and_uses_same_period_revision():
    symbol = "ZZV5GROW"
    as_of = datetime.date(2024, 6, 30)
    past1 = datetime.datetime(2024, 5, 1, tzinfo=datetime.timezone.utc)
    past2 = datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc)
    future = datetime.datetime(2024, 7, 1, tzinfo=datetime.timezone.utc)
    period_end = datetime.date(2025, 12, 31)
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.query(AnalystConsensusSnapshot).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        for observed, revenue, digest in (
            (past1, 100.0, "phase3-past-1"),
            (past2, 110.0, "phase3-past-2"),
            (future, 999.0, "phase3-future"),
        ):
            session.add(AnalystConsensusSnapshot(
                ticker_id=ticker_id, observed_at=observed, source="test",
                period_type="FY", period_end=period_end, revenue_mean=revenue,
                analyst_count=6, coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
                confidence="medium", content_hash=digest,
            ))
    item = V5PitInput(
        ticker_id=ticker_id, symbol=symbol, as_of=as_of,
        moic_inputs=SimpleNamespace(revenue_latest=80.0),
        raw_snapshot_id=1, raw_available_from=as_of,
        price_as_of=as_of, input_status="collected_with_data",
    )
    try:
        with session_scope() as session:
            feature_set = build_growth_feature_sets(
                session, [item], as_of=as_of, config=load_model_v5_config()
            )
        signal = next(
            signal for signal in feature_set[ticker_id].signals
            if signal.key == "consensus_revision"
        )
        assert signal.status == "applied"
        assert signal.value == pytest.approx(0.10)
        assert signal.evidence["latest_revenue_mean"] == 110.0
        assert signal.observed_at == past2
    finally:
        with session_scope() as session:
            session.query(AnalystConsensusSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()


def test_shadow_run_persists_feature_ablation_without_touching_v4(monkeypatch):
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        ticker = session.query(Ticker).order_by(Ticker.id).first()
        ticker_id, symbol = ticker.id, ticker.symbol
        v4_before = session.query(Score).count()
    item = V5PitInput(
        ticker_id=ticker_id, symbol=symbol, as_of=as_of,
        moic_inputs=SimpleNamespace(fcf_margin=0.08, net_debt=100.0),
        raw_snapshot_id=1, raw_available_from=as_of,
        price_as_of=as_of, input_status="collected_with_data",
    )
    feature_set = _features(_signal(
        "consensus_revision", 0.10, evidence={"years_to_period": 1.0}
    ))
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
        lambda *a, **k: {ticker_id: feature_set},
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
            impact = score.features["ablation"]["consensus_revision"]
            assert score.states["contract_version"] == "v5.phase4"
            assert score.states["state_updates_applied"] == ["consensus_revision"]
            assert impact["scenario_impact"]["expected_cagr"] > 0
            assert impact["status"] == "computed"
            assert run.metrics["applied_feature_counts"] == {"consensus_revision": 1}
            assert run.metrics["ablation_results"] == 1
            assert session.query(Score).count() == v4_before
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
