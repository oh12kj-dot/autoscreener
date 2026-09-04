"""WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md) regression tests.

``model_confidence`` was exactly ``0.5`` for every one of 1,157 scored
tickers (docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2), which made
RACR's ``ModelUncertainty`` term a constant multiple of ``ce_cagr`` rather
than independent information. Covers:

  - D-1/D-2: ``scoring/v5/reliability.py``'s pure functions (``freshness``,
    ``reliability_weight``, ``core_evidence_reliability``,
    ``base_confidence_for``, ``feature_confidence_delta``,
    ``decayed_reliability``) -- no DB session needed for these.
  - D-3: ``FeatureSpec`` no longer declares ``transform``/``winsorization``/
    ``sector_normalization`` (verified dead, deleted);
    ``freshness_half_life_days`` is still present and now wired into
    ``build_growth_feature_sets``'s reliability decay.
  - The end-to-end regression guard: a ``run_v5_shadow`` run over tickers
    with genuinely different evidence must NOT collapse ``model_confidence``
    to a single universe-wide value -- the exact failure mode this WP
    fixes. Mirrors ``test_v5_racr_wp_b2.py``'s DB-backed integration style
    (mock ``build_v5_pit_inputs``/``compute_moic`` only; real feature-set
    builders and real ``reliability.py`` run against a fresh, isolated set
    of tickers).
  - D-4: ``ModelFeatureValue`` rows are actually persisted, one per
    (run, ticker, feature), including the two always-present base rows.
"""

from __future__ import annotations

import datetime
import math
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import ModelFeatureValue, ModelRun, ModelScore, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.v5.feature_registry import FEATURE_REGISTRY, FeatureSpec
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.reliability import (
    Q_RECONCILE_INERT,
    base_confidence_for,
    core_evidence_reliability,
    decayed_reliability,
    feature_confidence_delta,
    freshness,
    reliability_weight,
)
from autoscreener.screening.financial_history import FinancialPeriod

AS_OF = datetime.date(2024, 6, 30)


# ---------------------------------------------------------------------------
# D-1: freshness / reliability_weight -- pure math.
# ---------------------------------------------------------------------------


def test_freshness_is_one_when_age_or_half_life_unknown():
    assert freshness(None, 90) == 1.0
    assert freshness(30, None) == 1.0
    assert freshness(30, 0) == 1.0
    assert freshness(-5, 90) == 1.0  # negative age is unknown too -> no penalty


def test_freshness_halves_at_the_half_life():
    assert freshness(90, 90) == pytest.approx(0.5, abs=1e-9)
    assert freshness(180, 90) == pytest.approx(0.25, abs=1e-9)


def test_freshness_is_monotonically_decreasing_in_age():
    values = [freshness(age, 200) for age in (0, 50, 100, 200, 400, 800)]
    assert values == sorted(values, reverse=True)
    assert values[0] == 1.0


def test_reliability_weight_multiplies_and_clamps_out_of_range_inputs():
    weight = reliability_weight(q_source=1.0, q_extract=1.0, q_pit=1.0, q_sample=1.0)
    assert weight == pytest.approx(1.0)
    # q_reconcile defaults to the named inert constant.
    assert Q_RECONCILE_INERT == 1.0
    # A caller bug (out-of-range q) must not silently exceed [0, 1].
    over = reliability_weight(q_source=2.0, q_extract=1.0, q_pit=1.0, q_sample=1.0)
    assert over == pytest.approx(1.0)
    under = reliability_weight(q_source=-1.0, q_extract=1.0, q_pit=1.0, q_sample=1.0)
    assert under == pytest.approx(0.0)
    decayed = reliability_weight(
        q_source=1.0, q_extract=1.0, q_pit=1.0, q_sample=1.0, age_days=90, half_life_days=90,
    )
    assert decayed == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# D-1: core_evidence_reliability / base_confidence_for.
# ---------------------------------------------------------------------------


def _period(period_end: datetime.date, **overrides) -> FinancialPeriod:
    base = dict(
        revenue=100.0, gross_profit=40.0, operating_income=20.0, net_income=15.0,
        operating_cash_flow=18.0, free_cash_flow=12.0, cash_and_equivalents=50.0,
        total_debt=30.0, shares_outstanding=10.0, total_assets=200.0,
    )
    base.update(overrides)
    return FinancialPeriod(period_end=period_end, **base)


def _item(**overrides) -> V5PitInput:
    defaults = dict(
        ticker_id=1, symbol="ZZ", as_of=AS_OF, moic_inputs=SimpleNamespace(),
        raw_snapshot_id=1, raw_available_from=AS_OF, price_as_of=AS_OF,
        input_status="collected_with_data",
        financial_annual=(_period(datetime.date(2024, 3, 31)),) * 4,
        price_row_count=756, price_first_date=datetime.date(2021, 6, 30),
        raw_is_valid=True, raw_validation_error_count=0,
    )
    defaults.update(overrides)
    return V5PitInput(**defaults)


def test_core_evidence_reliability_full_evidence_scores_high():
    config = load_model_v5_config().reliability
    evidence = core_evidence_reliability(_item(), as_of=AS_OF, config=config)
    assert evidence.q_source == pytest.approx(1.0)
    assert evidence.q_extract == pytest.approx(1.0)
    assert evidence.q_pit == pytest.approx(1.0)
    assert evidence.q_sample == pytest.approx(1.0)
    assert evidence.value > 0.5


def test_core_evidence_reliability_no_statements_scores_zero_extract():
    config = load_model_v5_config().reliability
    evidence = core_evidence_reliability(_item(financial_annual=()), as_of=AS_OF, config=config)
    assert evidence.q_extract == 0.0
    assert evidence.reporting_lag_days is None
    assert evidence.value == 0.0


def test_core_evidence_reliability_partial_statement_completeness_is_between():
    config = load_model_v5_config().reliability
    sparse = _period(datetime.date(2024, 3, 31), gross_profit=None, operating_cash_flow=None,
                      free_cash_flow=None, total_debt=None, total_assets=None)
    evidence = core_evidence_reliability(_item(financial_annual=(sparse,)), as_of=AS_OF, config=config)
    assert 0.0 < evidence.q_extract < 1.0


def test_core_evidence_reliability_short_price_history_lowers_q_sample():
    config = load_model_v5_config().reliability
    thin = core_evidence_reliability(_item(price_row_count=4), as_of=AS_OF, config=config)
    thick = core_evidence_reliability(_item(price_row_count=756), as_of=AS_OF, config=config)
    assert thin.q_sample < thick.q_sample


def test_core_evidence_reliability_invalid_raw_snapshot_lowers_q_source():
    config = load_model_v5_config().reliability
    valid = core_evidence_reliability(_item(), as_of=AS_OF, config=config)
    invalid = core_evidence_reliability(
        _item(raw_is_valid=False, raw_validation_error_count=3), as_of=AS_OF, config=config,
    )
    assert invalid.q_source < valid.q_source


def test_core_evidence_reliability_stale_price_lowers_q_pit():
    config = load_model_v5_config().reliability
    fresh = core_evidence_reliability(_item(price_as_of=AS_OF), as_of=AS_OF, config=config)
    stale = core_evidence_reliability(
        _item(price_as_of=AS_OF - datetime.timedelta(days=120)), as_of=AS_OF, config=config,
    )
    assert stale.q_pit < fresh.q_pit


def test_core_evidence_reliability_older_reporting_lag_decays_via_freshness():
    config = load_model_v5_config().reliability
    recent = core_evidence_reliability(
        _item(financial_annual=(_period(AS_OF - datetime.timedelta(days=66)),) * 4),
        as_of=AS_OF, config=config,
    )
    stale = core_evidence_reliability(
        _item(financial_annual=(_period(AS_OF - datetime.timedelta(days=612)),) * 4),
        as_of=AS_OF, config=config,
    )
    assert stale.value < recent.value


def test_base_confidence_for_unavailable_returns_configured_constant_and_no_evidence():
    config = load_model_v5_config()
    confidence, evidence = base_confidence_for(
        _item(), as_of=AS_OF, config=config, has_distribution=False,
    )
    assert confidence == config.reliability.unavailable_input_confidence
    assert evidence is None


def test_base_confidence_for_varies_with_evidence_quality_and_stays_in_configured_range():
    config = load_model_v5_config()
    strong, _ = base_confidence_for(_item(), as_of=AS_OF, config=config, has_distribution=True)
    weak, _ = base_confidence_for(
        _item(financial_annual=(), price_row_count=0, raw_is_valid=False,
              raw_validation_error_count=5, price_as_of=None),
        as_of=AS_OF, config=config, has_distribution=True,
    )
    assert weak < strong
    for value in (strong, weak):
        assert config.reliability.min_base_confidence <= value <= config.reliability.max_base_confidence


def test_base_confidence_for_does_not_collapse_across_a_small_varied_population():
    """The direct analogue of the diagnosed defect at the function level:
    three tickers with different evidence must not all land on the exact
    same confidence."""
    config = load_model_v5_config()
    items = [
        _item(ticker_id=1, financial_annual=(_period(datetime.date(2024, 3, 31)),) * 4,
              price_row_count=756),
        _item(ticker_id=2, financial_annual=(_period(datetime.date(2023, 6, 30)),) * 2,
              price_row_count=200, raw_validation_error_count=1),
        _item(ticker_id=3, financial_annual=(), price_row_count=10, raw_is_valid=False,
              raw_validation_error_count=3),
    ]
    confidences = {
        item.ticker_id: base_confidence_for(item, as_of=AS_OF, config=config, has_distribution=True)[0]
        for item in items
    }
    assert len(set(round(v, 9) for v in confidences.values())) == 3


# ---------------------------------------------------------------------------
# D-2: feature_confidence_delta -- shared penalty/bonus contract.
# ---------------------------------------------------------------------------


def _signal(**overrides):
    defaults = dict(
        key="consensus_revision", status="applied", coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
        runtime_enabled=True, applied=True, reliability=0.8, observed_at=None, value=0.1, evidence={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_feature_confidence_delta_penalizes_expected_but_missing_signals():
    signals = [
        _signal(coverage_status=CoverageStatus.NOT_COLLECTED, applied=False, status="not_collected"),
        _signal(coverage_status=CoverageStatus.COLLECTION_FAILED, applied=False, status="collection_failed"),
    ]
    assert feature_confidence_delta(signals) < 0


def test_feature_confidence_delta_ignores_runtime_disabled_signals():
    signals = [_signal(runtime_enabled=False, applied=False)]
    assert feature_confidence_delta(signals) == 0.0


def test_feature_confidence_delta_rewards_applied_evidence():
    """D-2: confidence must rise with evidence that actually entered the
    state, not only fall with missingness."""
    missing_only = feature_confidence_delta([
        _signal(coverage_status=CoverageStatus.NOT_COLLECTED, applied=False, status="not_collected"),
    ])
    applied_only = feature_confidence_delta([_signal(applied=True, reliability=0.9)])
    assert applied_only > 0
    assert applied_only > missing_only


def test_feature_confidence_delta_bonus_scales_with_signal_reliability():
    low = feature_confidence_delta([_signal(applied=True, reliability=0.2)])
    high = feature_confidence_delta([_signal(applied=True, reliability=0.95)])
    assert high > low > 0


def test_feature_confidence_delta_stays_within_bound():
    signals = [_signal(applied=True, reliability=1.0) for _ in range(50)]
    assert feature_confidence_delta(signals) <= 0.20 + 1e-9


# ---------------------------------------------------------------------------
# D-3: freshness_half_life_days wiring; dead fields removed.
# ---------------------------------------------------------------------------


def test_feature_spec_no_longer_declares_removed_dead_fields():
    for dead_field in ("transform", "winsorization", "sector_normalization"):
        assert dead_field not in FeatureSpec.__dataclass_fields__
    # freshness_half_life_days is the one field kept -- it now executes.
    assert "freshness_half_life_days" in FeatureSpec.__dataclass_fields__
    assert any(spec.freshness_half_life_days is not None for spec in FEATURE_REGISTRY)


def test_decayed_reliability_is_unchanged_without_half_life_or_observed_at():
    signal = _signal(reliability=0.7, observed_at=None)
    assert decayed_reliability(signal, half_life_days=None, as_of=AS_OF) == 0.7
    signal2 = _signal(reliability=0.7, observed_at=datetime.datetime(
        2024, 6, 1, tzinfo=datetime.timezone.utc,
    ))
    assert decayed_reliability(signal2, half_life_days=None, as_of=AS_OF) == 0.7


def test_decayed_reliability_decays_with_age_when_half_life_set():
    fresh_signal = _signal(reliability=0.8, observed_at=datetime.datetime(
        2024, 6, 29, tzinfo=datetime.timezone.utc,
    ))
    stale_signal = _signal(reliability=0.8, observed_at=datetime.datetime(
        2024, 1, 1, tzinfo=datetime.timezone.utc,
    ))
    fresh = decayed_reliability(fresh_signal, half_life_days=90, as_of=AS_OF)
    stale = decayed_reliability(stale_signal, half_life_days=90, as_of=AS_OF)
    assert stale < fresh < 0.8 + 1e-9
    assert stale < 0.8


# ---------------------------------------------------------------------------
# End-to-end: run_v5_shadow must not collapse model_confidence, and must
# persist ModelFeatureValue rows (D-4).
# ---------------------------------------------------------------------------


def test_run_v5_shadow_model_confidence_varies_with_real_evidence(monkeypatch):
    """The exact regression this WP exists to prevent: three tickers with
    genuinely different core evidence (statement recency/completeness,
    price-history depth, raw-snapshot validity) must not all be scored with
    identical `model_confidence`. Before WP-D this always failed (flat 0.5
    for every ready ticker, docs/racr_shadow_run_diagnostic_2026-09-04.md
    §3.2) regardless of how different the underlying tickers were."""
    symbols = ["ZZWPD1", "ZZWPD2", "ZZWPD3"]
    ticker_ids: list[int] = []
    with session_scope() as session:
        for symbol in symbols:
            old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
            if old is not None:
                session.delete(old)
                session.flush()
            ticker = Ticker(symbol=symbol, market="US")
            session.add(ticker)
            session.flush()
            ticker_ids.append(ticker.id)

    strong_periods = tuple(
        _period(datetime.date(2024, 4, 15) - datetime.timedelta(days=365 * i))
        for i in range(3, -1, -1)
    )
    weak_periods = tuple(
        _period(
            datetime.date(2023, 4, 1) - datetime.timedelta(days=365 * i),
            gross_profit=None, operating_cash_flow=None,
            free_cash_flow=None, total_debt=None, total_assets=None,
        )
        for i in range(1, -1, -1)
    )
    items = [
        V5PitInput(
            ticker_id=ticker_ids[0], symbol=symbols[0], as_of=AS_OF,
            moic_inputs=SimpleNamespace(variant=0, fcf_margin=0.08, net_debt=100.0), raw_snapshot_id=1,
            raw_available_from=AS_OF, price_as_of=AS_OF, input_status="collected_with_data",
            financial_annual=strong_periods, price_row_count=756,
            raw_is_valid=True, raw_validation_error_count=0,
        ),
        V5PitInput(
            ticker_id=ticker_ids[1], symbol=symbols[1], as_of=AS_OF,
            moic_inputs=SimpleNamespace(variant=1, fcf_margin=0.08, net_debt=100.0), raw_snapshot_id=2,
            raw_available_from=AS_OF, price_as_of=AS_OF, input_status="collected_with_data",
            financial_annual=weak_periods, price_row_count=180,
            raw_is_valid=True, raw_validation_error_count=1,
        ),
        V5PitInput(
            ticker_id=ticker_ids[2], symbol=symbols[2], as_of=AS_OF,
            moic_inputs=SimpleNamespace(variant=2, fcf_margin=0.08, net_debt=100.0), raw_snapshot_id=3,
            raw_available_from=AS_OF, price_as_of=AS_OF - datetime.timedelta(days=90),
            input_status="collected_with_data", financial_annual=(), price_row_count=5,
            raw_is_valid=False, raw_validation_error_count=3,
        ),
    ]

    def _fake_compute_moic(moic_inputs, *_args, **_kwargs):
        return SimpleNamespace(
            probability=0.04, expected_moic=2.0, median_moic=1.6,
            log_moic_mu=math.log(2.0) - 0.5 * 0.6**2, log_moic_sigma=0.6,
            survival_probability=0.9, initial_growth_rate=0.10,
            terminal_growth_rate=0.04, revenue_multiple=2.0, terminal_gross_margin=0.45,
            dilution_drag=1.05, projected_net_debt=20.0, current_ev_to_gross_profit=4.0,
            multiple_change=0.9, growth_fade_rate=0.7,
        )

    monkeypatch.setattr("autoscreener.scoring.v5.engine.build_v5_pit_inputs", lambda *a, **k: items)
    monkeypatch.setattr("autoscreener.scoring.v5.engine.cross_section_for", lambda *a, **k: object())
    monkeypatch.setattr("autoscreener.scoring.v5.engine.compute_moic", _fake_compute_moic)

    result = run_v5_shadow(AS_OF)
    run_id = result["run_id"]
    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            scores = session.query(ModelScore).filter_by(run_id=run_id).all()
            confidences = {float(score.confidence) for score in scores}
            assert len(confidences) == 3, (
                f"model_confidence collapsed: {confidences} (expected 3 distinct values "
                "for 3 tickers with different evidence -- this is exactly the "
                "docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2 defect)"
            )
            reliability_diag = run.metrics["reliability_diagnostics"]
            assert reliability_diag["model_confidence"]["distinct"] == 3
            assert reliability_diag["core_evidence_reliability"]["distinct"] == 3
            assert "model_confidence" not in run.metrics["objective_diagnostics"]["distribution_constant_fields"]

            # D-4: ModelFeatureValue rows persisted, including the two
            # always-present base rows for every ticker.
            feature_rows = session.query(ModelFeatureValue).filter_by(run_id=run_id).all()
            assert len(feature_rows) > 0
            by_ticker: dict[int, set[str]] = {}
            for row in feature_rows:
                by_ticker.setdefault(row.ticker_id, set()).add(row.feature_key)
            for ticker_id in ticker_ids:
                assert "base_financial_statements" in by_ticker[ticker_id]
                assert "price_history" in by_ticker[ticker_id]
            base_row = next(
                row for row in feature_rows
                if row.ticker_id == ticker_ids[0] and row.feature_key == "base_financial_statements"
            )
            assert base_row.reliability is not None
            assert base_row.source == "raw_snapshots"
    finally:
        with session_scope() as session:
            session.query(ModelFeatureValue).filter(
                ModelFeatureValue.ticker_id.in_(ticker_ids)
            ).delete(synchronize_session=False)
            session.query(ModelRun).filter_by(id=run_id).delete()
            for ticker_id in ticker_ids:
                session.query(Ticker).filter_by(id=ticker_id).delete()
