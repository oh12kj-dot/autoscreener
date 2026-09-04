from __future__ import annotations

import datetime
import math
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.config import load_model_v5_config, load_objectives_config
from autoscreener.db.models import ModelRun, ModelScore, ObjectiveScore, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.distribution import scenario_distribution
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.scenario import ReturnScenario, build_scenarios
from autoscreener.scoring.v5.state_model import build_future_state

client = TestClient(app)


def _seed_result(survival: float = 0.94):
    return SimpleNamespace(
        log_moic_mu=math.log(2.0) - 0.5 * 0.7**2,
        log_moic_sigma=0.7,
        survival_probability=survival,
        initial_growth_rate=0.15,
        terminal_growth_rate=0.04,
        revenue_multiple=2.4,
        terminal_gross_margin=0.42,
        dilution_drag=1.12,
        projected_net_debt=80.0,
        current_ev_to_gross_profit=5.0,
        multiple_change=0.75,
        growth_fade_rate=0.75,
    )


def _distribution(survival: float = 0.94) -> dict:
    config = load_model_v5_config()
    scenarios = build_scenarios(_seed_result(survival), confidence=0.5, config=config)
    return scenario_distribution(
        scenarios, horizon_years=7, target_moic=10.0, confidence=0.5
    )


def test_scenario_engine_preserves_conditional_mean_and_widens_low_confidence():
    config = load_model_v5_config()
    seed = _seed_result()
    high = build_scenarios(seed, confidence=1.0, config=config)
    low = build_scenarios(seed, confidence=0.0, config=config)
    seed_mean = math.exp(seed.log_moic_mu + seed.log_moic_sigma**2 / 2)
    assert sum(s.weight * s.conditional_expected_moic for s in high) == pytest.approx(seed_mean)
    assert sum(s.weight * s.conditional_expected_moic for s in low) == pytest.approx(seed_mean)
    assert low[1].log_sigma > high[1].log_sigma
    assert low[0].log_sigma > low[1].log_sigma


def test_phase2_distribution_contract_is_monotone_and_has_es():
    distribution = _distribution()
    assert distribution["status"] == "available"
    assert 0 <= distribution["p_moic_below_0_5"] <= distribution["p_moic_below_1_0"] <= 1
    assert 1 >= distribution["p_moic_2x"] >= distribution["p_moic_3x"] >= distribution["p_moic_5x"] >= distribution["p_moic_10x"] >= 0
    assert distribution["p10_moic"] <= distribution["p25_moic"] <= distribution["p50_moic"] <= distribution["p75_moic"] <= distribution["p90_moic"]
    assert 0 <= distribution["expected_shortfall_10pct"] <= distribution["p10_moic"]
    assert distribution["p_target"] == pytest.approx(distribution["p_moic_10x"])
    assert len(distribution["scenarios"]) == 3


def test_failure_atom_controls_quantile_and_expected_shortfall():
    distribution = _distribution(survival=0.85)
    assert distribution["p10_moic"] == 0.0
    assert distribution["expected_shortfall_10pct"] == 0.0
    assert distribution["p_moic_below_0_5"] >= 0.15


def test_distribution_objectives_are_separate_and_later_phase_ones_disabled():
    results = evaluate_objectives(_distribution(), load_objectives_config(), horizon_years=7)
    # WP-B(docs/racr_wp_b_output_contract_2026-09-04.md)で
    # `risk_adjusted_compounding` がenabledなshadow objectiveとして加わった。
    # この集合は「分布だけで計算できるobjectiveが評価され、後続phaseの入力を
    # 要るものはdisabledのまま」を固定するためのものであり、RACRは前者
    # ——分布のCE CAGRとtail lossだけで計算できる——なのでここに入るのが正しい。
    # deprecated扱いの `risk_adjusted` も、champion比較のためenabledのまま
    # 残す方針(config/objectives.yaml参照)なので引き続き含まれる。
    assert set(results) == {
        "ten_bagger", "expected_return", "risk_adjusted", "asymmetric",
        "capital_preservation", "risk_adjusted_compounding",
    }
    assert all(item.status == "available" for item in results.values())
    assert results["ten_bagger"].score_value == pytest.approx(_distribution()["p_target"])
    assert results["capital_preservation"].score_value == pytest.approx(
        1 - _distribution()["p_moic_below_1_0"]
    )


def test_state_contract_marks_later_phase_values_unsupported_not_zero():
    state = build_future_state(
        _seed_result(), SimpleNamespace(fcf_margin=None, net_debt=100.0),
        horizon_years=7, confidence=0.5,
    ).to_dict()
    assert state["economics"]["cash_conversion"]["status"] == "not_collected"
    assert state["economics"]["cash_conversion"]["value"] is None
    assert state["economics"]["reinvestment_efficiency"] == {
        "value": None, "status": "unsupported", "source": "phase4"
    }
    assert state["competing_risk"]["acquisition_probability"]["value"] is None


@pytest.fixture
def phase2_api_run():
    symbol = "ZZV5API"
    run_id = uuid.uuid4()
    as_of = datetime.date(2099, 2, 1)
    with session_scope() as session:
        existing = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if existing is not None:
            session.query(ModelRun).filter(ModelRun.id.in_(
                session.query(ModelScore.run_id).filter_by(ticker_id=existing.id)
            )).delete(synchronize_session=False)
            session.delete(existing)
    with session_scope() as session:
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        distribution = _distribution()
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash="phase2-api-test",
            as_of=as_of, mode="shadow", status="succeeded", population_count=1,
            started_at=datetime.datetime(2099, 2, 1, tzinfo=datetime.timezone.utc),
            finished_at=datetime.datetime(2099, 2, 1, 1, tzinfo=datetime.timezone.utc),
            metrics={"phase2_distributions": 1}, warnings=[],
        ))
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker_id, target_horizon_years=7,
            target_moic=10.0, distribution=distribution,
            states={"contract_version": "v5.phase2", "status": "seeded"},
            features={"registry_version": "phase2"}, confidence=0.5, warnings=[],
        ))
        session.add(ObjectiveScore(
            run_id=run_id, ticker_id=ticker_id, objective="ten_bagger",
            score_value=distribution["p_target"], rank=1,
            explanation={"status": "available", "formula": "P(MOIC >= target_moic)"},
        ))
    yield symbol, as_of, run_id
    with session_scope() as session:
        session.query(ModelRun).filter_by(id=run_id).delete()
        session.query(Ticker).filter_by(id=ticker_id).delete()


def test_phase2_api_exposes_typed_list_and_detail(phase2_api_run):
    symbol, as_of, run_id = phase2_api_run
    run = client.get("/api/v1/models/v5/runs/latest", params={"as_of": as_of.isoformat()})
    assert run.status_code == 200
    assert run.json()["run_id"] == str(run_id)

    listing = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "ten_bagger", "limit": 200},
    )
    assert listing.status_code == 200
    row = next(item for item in listing.json()["items"] if item["ticker"] == symbol)
    assert row["rank"] == 1
    assert row["distribution"]["expected_shortfall_10pct"] is not None

    detail = client.get(
        f"/api/v1/models/v5/scores/{symbol}", params={"as_of": as_of.isoformat()}
    )
    assert detail.status_code == 200
    # WP-B (docs/racr_wp_b_output_contract_2026-09-04.md): distribution
    # contract bumped to v5.racr1. `states`' own contract_version (set
    # above in the fixture) is a separate field owned by state_model.py,
    # unaffected by this bump.
    assert detail.json()["distribution"]["contract_version"] == "v5.racr1"
    assert detail.json()["objectives"][0]["objective"] == "ten_bagger"


def test_v5_api_rejects_disabled_objective(phase2_api_run):
    _, as_of, _ = phase2_api_run
    response = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "quality_compounder"},
    )
    assert response.status_code == 422
