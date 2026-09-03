from __future__ import annotations

import datetime
import uuid

import pytest

from autoscreener.backtest.v5_comparison import (
    ComparisonRecord,
    compare_v4_v5_same_day,
    date_block_bootstrap_ci,
    historical_feature_flags,
    run_v5_historical,
)
from autoscreener.config import load_model_v5_config
from autoscreener.db.models import (
    ForwardReturn,
    ModelRun,
    ModelScore,
    ModelV5ForwardReturn,
    Score,
    Ticker,
)
from autoscreener.db.session import session_scope
from autoscreener.scoring.forward_validation import run_forward_validation_v5
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY


# ---------------------------------------------------------------------------
# historical_feature_flags / run_v5_historical: Issue #3 section 25.
# ---------------------------------------------------------------------------

def test_historical_feature_flags_force_disables_pit_unsupported_features():
    config = load_model_v5_config()
    # Sanity: at least one PIT-unsupported feature is enabled by config today
    # (litigation), otherwise this test would not actually exercise the
    # override path.
    pit_unsupported = [k for k, spec in FEATURES_BY_KEY.items() if not spec.historical_backtest_supported]
    assert any(config.feature_flags.get(k, False) for k in pit_unsupported)

    overridden, forced_off = historical_feature_flags(config)
    for key in pit_unsupported:
        assert overridden[key] is False
    assert set(forced_off) == {
        k for k in pit_unsupported if config.feature_flags.get(k, FEATURES_BY_KEY[k].default_enabled)
    }


def test_historical_feature_flags_never_force_enables_anything():
    """The harness only ever removes coverage, never grants it -- a feature
    disabled by config stays disabled regardless of historical_backtest_
    supported."""
    config = load_model_v5_config()
    overridden, _ = historical_feature_flags(config)
    for key, enabled in config.feature_flags.items():
        if not enabled:
            assert overridden[key] is False


def test_run_v5_historical_records_forced_disabled_features(monkeypatch):
    captured = {}

    def _fake_run_v5_shadow(as_of, *, model_config=None, objectives_config=None):
        captured["feature_flags"] = dict(model_config.feature_flags)
        # A real run_id shape (run_v5_shadow always returns one for a
        # succeeded run) -- the mock must not use a non-UUID placeholder
        # now that run_v5_historical persists onto the run record by run_id.
        return {"status": "succeeded", "run_id": "00000000-0000-0000-0000-000000000000"}

    monkeypatch.setattr("autoscreener.backtest.v5_comparison.run_v5_shadow", _fake_run_v5_shadow)
    result = run_v5_historical(datetime.date(2024, 6, 30))
    assert result["historical_mode"] is True
    for key in result["forced_disabled_features"]:
        assert captured["feature_flags"][key] is False
        assert not FEATURES_BY_KEY[key].historical_backtest_supported


def test_run_v5_historical_persists_forced_off_onto_the_run_record():
    """Audit fix (2026-09-03, second Phase 7 re-review): a historical run
    must be distinguishable from an ordinary shadow run by reading
    ModelRun.metrics/warnings alone, without recomputing
    historical_feature_flags() and comparing config hashes by hand."""
    result = run_v5_historical(datetime.date(2024, 6, 30))
    run_id = result["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            assert run.metrics["historical_mode"] is True
            assert set(run.metrics["historical_forced_off_features"]) == set(
                result["forced_disabled_features"]
            )
            assert any("historical_mode" in w for w in run.warnings)
    finally:
        with session_scope() as session:
            session.query(ModelScore).filter_by(run_id=run_id).delete()
            session.query(ModelRun).filter_by(id=run_id).delete()


# ---------------------------------------------------------------------------
# compare_v4_v5_same_day: read-only, never a backtest, honest about gaps.
# ---------------------------------------------------------------------------

def test_compare_v4_v5_same_day_no_run_found_reports_honestly():
    with session_scope() as session:
        record = compare_v4_v5_same_day(session, datetime.date(1999, 1, 1))
    assert record.v5_run_id is None
    assert record.v5_population == 0
    assert record.not_a_backtest is True
    assert record.decision_input_only is True
    assert "no_succeeded_v5_run_found_for_date" in record.warnings


def test_compare_v4_v5_same_day_never_reads_or_implies_realized_return():
    """The comparison record's own shape and warnings must never claim a
    return/loss metric -- Issue #3 section 31: don't compare against
    outcomes that don't exist yet."""
    with session_scope() as session:
        record = compare_v4_v5_same_day(session, datetime.date(1999, 1, 1))
    payload = record.to_dict()
    assert "realized_return" not in str(payload.keys())
    assert any("not_a_backtest" in w or "return_metrics_not_computable" in w for w in record.warnings)


def test_compare_v4_v5_same_day_with_real_run_and_v4_scores():
    as_of = datetime.date(2024, 5, 15)
    with session_scope() as session:
        ticker = session.query(Ticker).order_by(Ticker.id).first()
        ticker_id = ticker.id
        run_id = uuid.uuid4()
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash="testhash", as_of=as_of,
            mode="shadow", status="succeeded", population_count=1,
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            metrics={"code_revision": {"commit": "abc", "dirty": False, "reason": None},
                     "feature_universe_coverage": {"incremental_roic": 0.9}},
            warnings=[],
        ))
        session.flush()
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker_id, target_horizon_years=7, target_moic=10.0,
            distribution={"status": "available"}, states={}, features={}, confidence=0.5,
            warnings=[],
        ))
        # v4 Score row on the same date for the same ticker, so the overlap
        # population is nonzero. Cleaned up in `finally`.
        old_v4 = session.query(Score).filter_by(
            ticker_id=ticker_id, score_date=as_of, scoring_version="test-v7"
        ).one_or_none()
        if old_v4 is not None:
            session.delete(old_v4)
        session.add(Score(
            ticker_id=ticker_id, score_date=as_of, scoring_version="test-v7", config_hash="v4hash",
            probability=0.02,
        ))
    try:
        with session_scope() as session:
            record = compare_v4_v5_same_day(session, as_of, v5_run_id=str(run_id))
        assert record.v5_run_id == str(run_id)
        assert record.v5_config_hash == "testhash"
        assert record.code_revision == {"commit": "abc", "dirty": False, "reason": None}
        assert record.v5_feature_coverage == {"incremental_roic": 0.9}
        assert record.v4_population >= 1
        assert record.not_a_backtest is True
    finally:
        with session_scope() as session:
            session.query(ModelScore).filter_by(run_id=run_id).delete()
            session.query(ModelRun).filter_by(id=run_id).delete()
            session.query(Score).filter_by(
                ticker_id=ticker_id, score_date=as_of, scoring_version="test-v7"
            ).delete()


# ---------------------------------------------------------------------------
# date_block_bootstrap_ci: refuses below the minimum date count.
# ---------------------------------------------------------------------------

def test_bootstrap_ci_refuses_below_minimum_dates():
    # The real current v5 evaluation-date count (9, all of universe_snapshots'
    # 2026-08-23..2026-09-02 window) is used directly, to keep this test
    # anchored to the actual measured constraint rather than an arbitrary
    # small number.
    per_date = {f"2026-08-{23 + i:02d}": 0.01 * i for i in range(9)}
    result = date_block_bootstrap_ci(per_date)
    assert result["status"] == "insufficient_dates"
    assert result["available_dates"] == 9
    assert result["ci_low"] is None and result["ci_high"] is None


def test_bootstrap_ci_computes_with_enough_dates():
    per_date = {f"d{i}": 0.01 for i in range(40)}
    result = date_block_bootstrap_ci(per_date, iterations=200, min_dates=30)
    assert result["status"] == "computed"
    assert result["ci_low"] <= result["mean"] <= result["ci_high"]


# ---------------------------------------------------------------------------
# run_forward_validation_v5: append-only, reuses v4's settlement logic,
# never touches v4's own scores/forward_returns.
# ---------------------------------------------------------------------------

def test_forward_validation_v5_never_touches_v4_tables():
    with session_scope() as session:
        v4_forward_before = session.query(ForwardReturn).count()
        v4_scores_before = session.query(Score).count()
    # A far-future as_of_date so cutoff selects nothing real -- this call
    # must be a safe, cheap no-op given the current real run history (no
    # succeeded ModelRun is old enough to have matured yet, matching the
    # Phase 7 doc's measured finding).
    counts = run_forward_validation_v5(datetime.date(2026, 9, 3))
    assert isinstance(counts["computed"], int)
    with session_scope() as session:
        assert session.query(ForwardReturn).count() == v4_forward_before
        assert session.query(Score).count() == v4_scores_before


def test_forward_validation_v5_settles_a_matured_synthetic_run():
    as_of = datetime.date(2020, 1, 2)  # old enough to have matured for 1M
    with session_scope() as session:
        ticker = session.query(Ticker).order_by(Ticker.id).first()
        ticker_id = ticker.id
        run_id = uuid.uuid4()
        session.add(ModelRun(
            id=run_id, model_version="v5", config_hash="testhash2", as_of=as_of,
            mode="shadow", status="succeeded", population_count=1,
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            metrics={}, warnings=[],
        ))
        session.flush()
        session.add(ModelScore(
            run_id=run_id, ticker_id=ticker_id, target_horizon_years=7, target_moic=10.0,
            distribution={"status": "available"}, states={}, features={}, confidence=0.5,
            warnings=[],
        ))
    try:
        counts = run_forward_validation_v5(datetime.date(2020, 3, 1))  # ~2 months later -> 1M matured
        assert counts["computed"] + counts["missing_price"] + counts["not_matured"] >= 1
        with session_scope() as session:
            rows = session.query(ModelV5ForwardReturn).filter_by(run_id=run_id).all()
            for row in rows:
                assert row.horizon in {"1M", "3M", "6M", "1Y", "3Y", "5Y", "7Y"}
                assert row.base_date == as_of
    finally:
        with session_scope() as session:
            session.query(ModelV5ForwardReturn).filter_by(run_id=run_id).delete()
            session.query(ModelScore).filter_by(run_id=run_id).delete()
            session.query(ModelRun).filter_by(id=run_id).delete()
