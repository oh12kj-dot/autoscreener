"""WP-C (docs/racr_wp_c_api_ui_2026-09-04.md) API contract tests.

Covers the API-facing half of the plan's WP-C row: that the WP-B
distribution fields (RACR inputs, permanent-loss/MDD ``unavailable``
markers) actually reach the HTTP response unmodified, that the new
``/models/v5/scores`` filters behave, and -- the requirement the whole work
package exists for -- that a run scored before the RACR contract existed
is reported as "objective not computed for this run", never rendered as an
indistinguishable empty ranking table.

Uses a real Postgres session (``TEST_DATABASE_URL``, enforced fail-closed by
``tests/conftest.py``), matching the pattern in ``test_v5_phase2.py``'s
``phase2_api_run`` fixture.
"""

from __future__ import annotations

import datetime
import math
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.config import load_model_v5_config
from autoscreener.db.models import ModelRun, ModelScore, ObjectiveScore, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.distribution import scenario_distribution
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.scenario import build_scenarios

client = TestClient(app)


def _seed_result(survival: float = 0.9, *, mu_moic: float = 2.0, sigma: float = 0.7):
    return SimpleNamespace(
        log_moic_mu=math.log(mu_moic) - 0.5 * sigma**2,
        log_moic_sigma=sigma,
        survival_probability=survival,
    )


def _distribution(*, survival: float = 0.9, mu_moic: float = 2.0, confidence: float = 0.5) -> dict:
    config = load_model_v5_config()
    scenarios = build_scenarios(_seed_result(survival, mu_moic=mu_moic), confidence=confidence, config=config)
    return scenario_distribution(scenarios, horizon_years=7, target_moic=10.0, confidence=confidence)


def _delete_symbol(symbol: str) -> None:
    with session_scope() as session:
        existing = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if existing is not None:
            session.query(ModelRun).filter(
                ModelRun.id.in_(session.query(ModelScore.run_id).filter_by(ticker_id=existing.id))
            ).delete(synchronize_session=False)
            session.delete(existing)


@pytest.fixture
def wp_c_run():
    """One run with two tickers scored on both ``ten_bagger`` and
    ``risk_adjusted_compounding`` -- the "current contract" case."""
    symbols = ["ZZV5WPCA", "ZZV5WPCB"]
    for s in symbols:
        _delete_symbol(s)
    run_id = uuid.uuid4()
    as_of = datetime.date(2098, 3, 1)
    from autoscreener.config import load_objectives_config

    objectives_config = load_objectives_config()
    ticker_ids: dict[str, int] = {}
    with session_scope() as session:
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash="wp-c-test",
            as_of=as_of, mode="shadow", status="succeeded", population_count=2,
            started_at=datetime.datetime(2098, 3, 1, tzinfo=datetime.timezone.utc),
            finished_at=datetime.datetime(2098, 3, 1, 1, tzinfo=datetime.timezone.utc),
            metrics={}, warnings=[],
        ))
        # High-confidence, high-quality ticker in "Technology".
        ticker_a = Ticker(symbol=symbols[0], market="US", sector="Technology")
        session.add(ticker_a)
        session.flush()
        ticker_ids[symbols[0]] = ticker_a.id
        dist_a = _distribution(survival=0.95, mu_moic=4.0, confidence=0.9)
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker_a.id, target_horizon_years=7, target_moic=10.0,
            distribution=dist_a, states={}, features={}, confidence=0.9, warnings=[],
        ))
        # Low-confidence, low-quality ticker in "Healthcare".
        ticker_b = Ticker(symbol=symbols[1], market="US", sector="Healthcare")
        session.add(ticker_b)
        session.flush()
        ticker_ids[symbols[1]] = ticker_b.id
        dist_b = _distribution(survival=0.5, mu_moic=1.1, confidence=0.2)
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker_b.id, target_horizon_years=7, target_moic=10.0,
            distribution=dist_b, states={}, features={}, confidence=0.2, warnings=[],
        ))
        for symbol, dist, rank in ((symbols[0], dist_a, 1), (symbols[1], dist_b, 2)):
            for objective_name in ("ten_bagger", "risk_adjusted_compounding"):
                result = evaluate_objectives(
                    dist, objectives_config, horizon_years=7
                )[objective_name]
                session.add(ObjectiveScore(
                    run_id=run_id, ticker_id=ticker_ids[symbol], objective=objective_name,
                    score_value=result.score_value, rank=rank,
                    # Matches engine.py's real persistence shape (evaluate_objectives'
                    # ObjectiveResult.status lives alongside its own explanation
                    # dict, not inside it -- engine.py merges the two before
                    # writing the row; routes.py's detail endpoint reads
                    # "status" back out of that merged dict).
                    explanation={"status": result.status, **result.explanation},
                ))
    yield symbols, as_of, run_id
    for s in symbols:
        _delete_symbol(s)


@pytest.fixture
def pre_racr_run():
    """A run scored only with ``ten_bagger`` -- simulates a run persisted
    before the risk_adjusted_compounding contract existed (every real
    ``model_scores`` row before 2026-09-04, per the WP-C task brief)."""
    symbol = "ZZV5WPCPRE"
    _delete_symbol(symbol)
    run_id = uuid.uuid4()
    as_of = datetime.date(2098, 3, 2)
    with session_scope() as session:
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash="wp-c-pre-racr-test",
            as_of=as_of, mode="shadow", status="succeeded", population_count=1,
            started_at=datetime.datetime(2098, 3, 2, tzinfo=datetime.timezone.utc),
            finished_at=datetime.datetime(2098, 3, 2, 1, tzinfo=datetime.timezone.utc),
            metrics={}, warnings=[],
        ))
        ticker = Ticker(symbol=symbol, market="US", sector="Industrials")
        session.add(ticker)
        session.flush()
        dist = _distribution()
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker.id, target_horizon_years=7, target_moic=10.0,
            distribution=dist, states={}, features={}, confidence=0.5, warnings=[],
        ))
        session.add(ObjectiveScore(
            run_id=run_id, ticker_id=ticker.id, objective="ten_bagger",
            score_value=dist["p_target"], rank=1,
            explanation={"status": "available"},
        ))
    yield symbol, as_of, run_id
    _delete_symbol(symbol)


# ---------------------------------------------------------------------------
# Distribution fields flow end-to-end through the API (WP-B -> WP-C wiring).
# ---------------------------------------------------------------------------

def test_racr_distribution_fields_reach_the_api_response(wp_c_run):
    symbols, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "risk_adjusted_compounding", "limit": 50},
    )
    assert r.status_code == 200
    body = r.json()
    row = next(item for item in body["items"] if item["ticker"] == symbols[0])
    dist = row["distribution"]
    # Computed fields actually present (available distribution).
    assert dist["ce_cagr"] is not None
    assert dist["p_cagr_above_15"] is not None
    assert dist["p_cagr_above_20"] is not None
    assert dist["p_cagr_above_25"] is not None
    # Never-implemented-yet fields: always None, always carry a reason.
    # This is the single most important assertion in this file -- the
    # entire WP-C task exists to stop these from ever reading as 0.
    for field, reason_field in (
        ("p_permanent_loss", "p_permanent_loss_unavailable_reason"),
        ("expected_max_drawdown", "expected_max_drawdown_unavailable_reason"),
        ("p_mdd_above_30", "p_mdd_above_30_unavailable_reason"),
        ("p_mdd_above_50", "p_mdd_above_50_unavailable_reason"),
        ("p_mdd_above_70", "p_mdd_above_70_unavailable_reason"),
        ("recovery_time_median", "recovery_time_median_unavailable_reason"),
    ):
        assert dist[field] is None, f"{field} must stay None, never a fabricated number"
        assert dist[reason_field], f"{field} must carry a machine-readable unavailable_reason"


def test_racr_objective_score_reports_omitted_terms_through_the_detail_api(wp_c_run):
    symbols, as_of, _ = wp_c_run
    r = client.get(f"/api/v1/models/v5/scores/{symbols[0]}", params={"as_of": as_of.isoformat()})
    assert r.status_code == 200
    body = r.json()
    racr = next(o for o in body["objectives"] if o["objective"] == "risk_adjusted_compounding")
    assert racr["status"] == "available"
    assert racr["explanation"]["omitted_terms"] == ["drawdown", "permanent_loss"]


# ---------------------------------------------------------------------------
# Filters.
# ---------------------------------------------------------------------------

def test_list_v5_scores_min_confidence_filter(wp_c_run):
    symbols, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "ten_bagger", "min_confidence": 0.5},
    )
    assert r.status_code == 200
    body = r.json()
    tickers = {item["ticker"] for item in body["items"]}
    assert symbols[0] in tickers  # confidence 0.9
    assert symbols[1] not in tickers  # confidence 0.2
    assert body["total"] == 1


def test_list_v5_scores_sector_filter(wp_c_run):
    symbols, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "ten_bagger", "sector": "Healthcare"},
    )
    assert r.status_code == 200
    tickers = {item["ticker"] for item in r.json()["items"]}
    assert symbols[1] in tickers
    assert symbols[0] not in tickers


def test_list_v5_scores_min_p_cagr_above_20_filter_excludes_the_weak_ticker(wp_c_run):
    symbols, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "ten_bagger", "min_p_cagr_above_20": 0.5},
    )
    assert r.status_code == 200
    tickers = {item["ticker"] for item in r.json()["items"]}
    # ticker B (mu_moic=1.1, survival=0.5) cannot plausibly clear a 50%
    # probability of a 20%/yr CAGR; ticker A (mu_moic=4.0) very plausibly can.
    assert symbols[1] not in tickers


def test_no_filter_over_unavailable_metrics_is_accepted(wp_c_run):
    """The plan is explicit: never invent a filter over a metric that is
    `unavailable` (permanent loss / MDD). Asserting the query param is
    silently ignored (FastAPI drops unknown query params) rather than
    accepted and acted upon -- i.e. there is no `max_permanent_loss` or
    `max_p_mdd_above_50` parameter wired to anything."""
    import inspect

    from autoscreener.api.routes import list_v5_scores

    params = set(inspect.signature(list_v5_scores).parameters)
    assert "max_permanent_loss" not in params
    assert "max_p_mdd_above_50" not in params


# ---------------------------------------------------------------------------
# The three empty-states (task brief's core requirement for the ranking list).
# ---------------------------------------------------------------------------

def test_objective_computed_for_run_true_for_current_contract_run(wp_c_run):
    _, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "risk_adjusted_compounding"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["objective_computed_for_run"] is True
    assert body["total"] > 0


def test_objective_computed_for_run_false_for_pre_racr_run(pre_racr_run):
    """The core WP-C regression case: a run scored before the RACR
    contract existed must not render RACR as an empty "no candidates
    qualified" table -- it must be flagged as objective-not-computed."""
    _, as_of, _ = pre_racr_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={"as_of": as_of.isoformat(), "objective": "risk_adjusted_compounding"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["objective_computed_for_run"] is False
    assert body["total"] == 0
    assert body["items"] == []


def test_objective_computed_for_run_true_but_genuinely_empty_when_filters_exclude_everything(wp_c_run):
    """Distinguishes "objective not computed" from "filters/universe
    legitimately yield zero rows" -- both can show total==0, but only the
    first should ever be read as "wrong objective for this run"."""
    _, as_of, _ = wp_c_run
    r = client.get(
        "/api/v1/models/v5/scores",
        params={
            "as_of": as_of.isoformat(),
            "objective": "risk_adjusted_compounding",
            "min_confidence": 0.999999,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["objective_computed_for_run"] is True
    assert body["total"] == 0
    assert body["items"] == []
