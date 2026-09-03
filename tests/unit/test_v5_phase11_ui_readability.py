"""Phase 11 (docs/model_v5_phase11_*.md) regression tests.

Found only by actually launching the app and looking at it (per the
coordinator's explicit instruction that static diffing/build-passing is
not sufficient): a same-day ``run-v5-shadow`` invocation for a date whose
``universe_snapshots`` row does not exist yet legitimately "succeeds"
with ``population_count = 0``. Ordering purely by ``as_of DESC`` let such
an empty run mask a real, populated run from a prior date on every v5 UI
surface -- confirmed live via ``/api/v1/models/v5/runs/latest`` and
``/api/v1/models/v5/validation-status`` both returning the empty run
before this fix.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.db.models import ModelRun, ModelScore, Ticker
from autoscreener.db.session import session_scope

client = TestClient(app)


def _make_run(*, as_of: datetime.date, population_count: int) -> uuid.UUID:
    run_id = uuid.uuid4()
    with session_scope() as session:
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash=f"phase11-test-{run_id.hex[:8]}",
            as_of=as_of, mode="shadow", status="succeeded",
            population_count=population_count,
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            metrics={}, warnings=[],
        ))
    return run_id


def test_latest_v5_run_prefers_populated_run_over_a_later_empty_one():
    """The exact bug: a later, empty run must not shadow an earlier,
    populated one on the endpoint the ranking/detail UI actually calls
    with no explicit as_of (i.e. "give me whatever's current")."""
    populated_run_id = _make_run(as_of=datetime.date(2099, 3, 1), population_count=500)
    empty_run_id = _make_run(as_of=datetime.date(2099, 3, 2), population_count=0)
    try:
        response = client.get("/api/v1/models/v5/runs/latest")
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == str(populated_run_id)
        assert body["population_count"] == 500
        assert body["as_of"] == "2099-03-01"
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter(
                ModelRun.id.in_([populated_run_id, empty_run_id])
            ).delete(synchronize_session=False)


def test_latest_v5_run_falls_back_to_empty_run_when_nothing_else_exists():
    """If literally no non-empty run exists in the requested window, still
    return something (an honest empty state) rather than 404ing when a run
    row does exist. Uses a date long before any real v5 run (earliest real
    universe_snapshots start 2026-08-23) so the `as_of <= ...` filter can't
    accidentally match real data and mask the case under test."""
    empty_run_id = _make_run(as_of=datetime.date(2020, 1, 5), population_count=0)
    try:
        response = client.get(
            "/api/v1/models/v5/runs/latest", params={"as_of": "2020-01-05"}
        )
        assert response.status_code == 200
        assert response.json()["run_id"] == str(empty_run_id)
        assert response.json()["population_count"] == 0
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=empty_run_id).delete()


def test_validation_status_latest_run_also_prefers_populated_run():
    """validation-status used to run its own independent, unfixed query
    for latest_run -- confirmed live via the browser that ValidationPage
    showed the empty run even after the ranking endpoint was fixed. This
    guards against that duplication regressing again."""
    populated_run_id = _make_run(as_of=datetime.date(2099, 3, 10), population_count=42)
    empty_run_id = _make_run(as_of=datetime.date(2099, 3, 11), population_count=0)
    try:
        response = client.get("/api/v1/models/v5/validation-status")
        assert response.status_code == 200
        latest_run = response.json()["latest_run"]
        assert latest_run is not None
        assert latest_run["run_id"] == str(populated_run_id)
        assert latest_run["population_count"] == 42
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter(
                ModelRun.id.in_([populated_run_id, empty_run_id])
            ).delete(synchronize_session=False)


def test_score_detail_endpoint_uses_the_populated_latest_run_not_an_empty_later_one():
    """End-to-end: a ticker scored only in the populated (earlier) run
    must still be found via the no-as_of "latest" resolution, not 404
    just because a later, empty run exists."""
    symbol = "ZZV5PHASE11"
    with session_scope() as session:
        existing = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if existing is not None:
            session.delete(existing)
    populated_run_id = _make_run(as_of=datetime.date(2099, 3, 20), population_count=1)
    empty_run_id = _make_run(as_of=datetime.date(2099, 3, 21), population_count=0)
    ticker_id = None
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            ticker_id = ticker.id
            from autoscreener.scoring.v5.distribution import unavailable_distribution
            session.add(ModelScore(
                run_id=populated_run_id, ticker_id=ticker_id, target_horizon_years=7,
                target_moic=10.0,
                distribution=unavailable_distribution(target_moic=10.0, confidence=0.0),
                states={"contract_version": "v5.phase2", "status": "unavailable"},
                features={"registry_version": "phase11-test", "ablation": {}}, confidence=0.0, warnings=[],
            ))
        response = client.get(f"/api/v1/models/v5/scores/{symbol}")
        assert response.status_code == 200
        assert response.json()["run"]["run_id"] == str(populated_run_id)
    finally:
        with session_scope() as session:
            session.query(ModelScore).filter_by(ticker_id=ticker_id).delete()
            session.query(ModelRun).filter(
                ModelRun.id.in_([populated_run_id, empty_run_id])
            ).delete(synchronize_session=False)
            session.query(Ticker).filter_by(id=ticker_id).delete()
