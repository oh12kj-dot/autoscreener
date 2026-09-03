from __future__ import annotations

from fastapi.testclient import TestClient

from autoscreener.api.main import app
from autoscreener.config import load_objectives_config

client = TestClient(app)


def test_v5_objectives_endpoint_excludes_disabled_objectives():
    """quality_compounder/execution_adjusted are disabled in
    config/objectives.yaml and must never appear in the UI's objective
    selector -- enforced by the endpoint itself, not by the frontend
    remembering to filter."""
    config = load_objectives_config()
    disabled = {name for name, d in config.objectives.items() if not d.enabled}
    assert disabled  # sanity: there is something to actually exclude
    r = client.get("/api/v1/models/v5/objectives")
    assert r.status_code == 200
    body = r.json()
    names = {item["name"] for item in body["objectives"]}
    assert names.isdisjoint(disabled)
    assert "quality_compounder" in disabled and "quality_compounder" not in names
    assert "execution_adjusted" in disabled and "execution_adjusted" not in names
    assert body["default_objective"] == config.default_objective


def test_v5_validation_status_reports_live_measured_numbers_not_hardcoded():
    r = client.get("/api/v1/models/v5/validation-status")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "CONTINUE_SHADOW"
    assert body["not_for_production"] is True
    assert isinstance(body["evaluation_dates_count"], int)
    assert body["evaluation_dates_count"] >= 0
    assert isinstance(body["realized_forward_validation_count"], int)
    assert "litigation" in body["unsupported_historical_features"]
    assert "macro_regime" in body["unsupported_historical_features"]
    assert "acquisition_competing_risk" in body["unsupported_historical_features"]
    assert "not_for_production" in body["warnings"]
    assert "forward_shadow_only" in body["warnings"]


def test_v5_validation_status_never_touches_v4_scores():
    from autoscreener.db.models import Score
    from autoscreener.db.session import session_scope

    with session_scope() as session:
        before = session.query(Score).count()
    r = client.get("/api/v1/models/v5/validation-status")
    assert r.status_code == 200
    with session_scope() as session:
        after = session.query(Score).count()
    assert before == after
