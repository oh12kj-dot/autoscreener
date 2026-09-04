from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from autoscreener.config import (
    ConfigSchemaError,
    load_model_v5_config,
    load_objectives_config,
)
from autoscreener.db.models import (
    ModelRun,
    ModelScore,
    PriceSnapshot,
    RawSnapshot,
    Score,
    Ticker,
    UniverseSnapshot,
)
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.distribution import base_distribution
from autoscreener.scoring.v5.engine import run_v5_shadow, v5_config_hash
from autoscreener.scoring.v5.feature_registry import (
    FEATURE_REGISTRY,
    feature_registry_payload,
    validate_feature_flags,
)
from autoscreener.scoring.v5.inputs import V5PitInput, build_v5_pit_inputs


def _result():
    return SimpleNamespace(
        probability=0.04,
        expected_moic=2.5,
        median_moic=1.9,
        log_moic_mu=0.64,
        log_moic_sigma=0.75,
        survival_probability=0.91,
        initial_growth_rate=0.12,
        terminal_growth_rate=0.04,
        revenue_multiple=2.0,
        terminal_gross_margin=0.45,
        dilution_drag=1.1,
        projected_net_debt=50.0,
        current_ev_to_gross_profit=4.0,
        multiple_change=0.8,
        growth_fade_rate=0.75,
    )


def test_v5_configs_and_registry_are_reproducible():
    model = load_model_v5_config()
    objectives = load_objectives_config()
    assert model.mode == "shadow"
    assert objectives.default_objective == "ten_bagger"
    assert v5_config_hash(model, objectives) == v5_config_hash(model, objectives)
    assert all(spec.key and spec.source and spec.target_state for spec in FEATURE_REGISTRY)
    assert feature_registry_payload(model.feature_flags)[0]["enabled"] is True


def test_v5_config_rejects_invalid_scenario_weights(tmp_path):
    path = tmp_path / "model_v5.yaml"
    path.write_text(
        "enabled: true\nmode: shadow\nmodel_version: v5\n"
        "scenario_weights: {downside: 0.4, base: 0.4, upside: 0.4}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigSchemaError):
        load_model_v5_config(path)


def test_feature_registry_rejects_unknown_config_flag():
    with pytest.raises(ValueError, match="unknown Model v5 feature"):
        validate_feature_flags({"does_not_exist": True})


def test_phase1_distribution_is_explicitly_base_only_or_unavailable():
    available = base_distribution(_result(), target_moic=10.0)
    assert available["status"] == "base_only"
    assert available["source_model_version"] == "v4"
    assert available["p_target"] == pytest.approx(0.04)
    unavailable = base_distribution(None, target_moic=10.0)
    assert unavailable["status"] == "unavailable"
    assert unavailable["p_target"] is None


def test_disabled_v5_config_is_a_no_write_rollback_switch():
    config = load_model_v5_config().model_copy(update={"enabled": False})
    with session_scope() as session:
        before = session.query(ModelRun).count()
    result = run_v5_shadow(datetime.date(2024, 6, 30), model_config=config)
    with session_scope() as session:
        assert session.query(ModelRun).count() == before
    assert result == {
        "status": "skipped",
        "reason": "disabled_by_config",
        "population": 0,
        "base_distributions": 0,
        "empty_distributions": 0,
    }


def test_v5_pit_builder_never_reads_future_snapshot_or_price():
    symbol = "ZZV5PIT"
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.query(UniverseSnapshot).filter_by(ticker_id=old.id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=old.id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(UniverseSnapshot(snapshot_date=as_of, ticker_id=ticker_id, included=True))
        past = RawSnapshot(
            ticker_id=ticker_id, snapshot_date=datetime.date(2024, 6, 1), source="test",
            payload={}, content_hash="v5-past", last_seen_date=as_of,
            available_from=datetime.date(2024, 6, 1), is_valid=True,
        )
        future = RawSnapshot(
            ticker_id=ticker_id, snapshot_date=datetime.date(2024, 7, 1), source="test",
            payload={}, content_hash="v5-future", last_seen_date=datetime.date(2024, 7, 1),
            available_from=datetime.date(2024, 7, 1), is_valid=True,
        )
        session.add_all([past, future])
        session.flush()
        past_id = past.id
        session.add_all([
            PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2024, 6, 28), close=10),
            PriceSnapshot(ticker_id=ticker_id, trade_date=datetime.date(2024, 7, 1), close=99),
        ])

    try:
        with session_scope() as session:
            item = next(row for row in build_v5_pit_inputs(session, as_of=as_of) if row.symbol == symbol)
            assert item.raw_snapshot_id == past_id
            assert item.raw_available_from <= as_of
            assert item.price_as_of == datetime.date(2024, 6, 28)
            assert item.evidence()["pit_rules"]["raw_snapshot"] == "available_from <= as_of"
    finally:
        with session_scope() as session:
            session.query(UniverseSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(RawSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()


def test_v5_shadow_persists_separately_without_touching_v4(monkeypatch):
    # WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):以前は
    # 「DBに既にTickerが1件ある」前提で `.first()` を読んでいたが、隔離済み
    # テストDBは0件から始まるため `None` を返し `.id` で落ちていた。
    # このテストの実体はV5 shadow runの永続化であり、対象Tickerがどれかは
    # 無関係なので自前で1件作る(`test_v5_pit_builder_never_reads_future_snapshot_or_price`
    # と同じ後始末パターン)。
    symbol = "ZZV5SHADOW"
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
        ticker_id=ticker_id,
        symbol=symbol,
        as_of=datetime.date(2024, 6, 30),
        moic_inputs=SimpleNamespace(fcf_margin=0.08, net_debt=100.0),
        raw_snapshot_id=123,
        raw_available_from=datetime.date(2024, 6, 1),
        price_as_of=datetime.date(2024, 6, 28),
        input_status="collected_with_data",
    )
    monkeypatch.setattr("autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: [item])
    monkeypatch.setattr("autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object())
    monkeypatch.setattr("autoscreener.scoring.v5.engine.compute_moic", lambda *a, **k: _result())

    result = run_v5_shadow(datetime.date(2024, 6, 30))
    run_id = result["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            assert run is not None
            assert run.status == "succeeded"
            assert run.mode == "shadow"
            assert run.population_count == 1
            score = session.query(ModelScore).filter_by(run_id=run_id).one()
            assert score.distribution["status"] == "available"
            # WP-B (docs/racr_wp_b_output_contract_2026-09-04.md): distribution
            # contract bumped to v5.racr1.
            assert score.distribution["contract_version"] == "v5.racr1"
            assert score.states["state_updates_applied"] == []
            assert float(score.confidence) == pytest.approx(0.5)
            assert session.query(Score).count() == v4_before
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()
