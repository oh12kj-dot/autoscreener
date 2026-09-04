from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from autoscreener.config import load_model_v5_config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import (
    ModelRun, ModelScore, RawSnapshot, Score, Ticker, UniverseSnapshot, XbrlFact,
)
from autoscreener.db.session import session_scope
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.v5.growth import GrowthFeatureSet
from autoscreener.scoring.v5.inputs import V5PitInput, build_v5_pit_inputs
from autoscreener.scoring.v5.quality import (
    QualityFeatureSet,
    QualitySignal,
    apply_quality_features,
    build_quality_feature_sets,
)
from autoscreener.scoring.v5.scenario import build_scenarios
from autoscreener.screening.financial_history import FinancialPeriod, build_financial_history


def _period(period_end: datetime.date, **kwargs) -> FinancialPeriod:
    return FinancialPeriod(period_end=period_end, **kwargs)


def _result(initial: float = 0.20, fade: float = 0.75, terminal: float = 0.04):
    return SimpleNamespace(
        initial_growth_rate=initial, growth_fade_rate=fade, terminal_growth_rate=terminal,
    )


def _seed_result(survival: float = 0.94):
    import math
    return SimpleNamespace(
        log_moic_mu=math.log(2.0) - 0.5 * 0.7**2,
        log_moic_sigma=0.7,
        survival_probability=survival,
    )


def _signal(
    key: str, value: float | None, *, applied: bool = True,
    reliability: float = 0.9, evidence: dict | None = None,
    coverage: str = CoverageStatus.COLLECTED_WITH_DATA,
) -> QualitySignal:
    return QualitySignal(
        key=key, status="applied" if applied else str(coverage),
        coverage_status=coverage, runtime_enabled=True, applied=applied,
        reliability=reliability, observed_at=None, value=value,
        evidence=evidence or {},
    )


def _features(*signals: QualitySignal) -> QualityFeatureSet:
    return QualityFeatureSet(tuple(signals), {signal.key: 1.0 for signal in signals})


# ---------------------------------------------------------------------------
# FinancialPeriod extension (handoff 4.2 option (a)): additive, no regression.
# ---------------------------------------------------------------------------

def test_financial_period_gains_accounting_fields_without_changing_existing_ones():
    payload = {
        "info": {"currency": "USD", "financialCurrency": "USD"},
        "income_stmt": {
            "Total Revenue": {"2023-12-31": 210.0, "2024-12-31": 300.0},
            "Gross Profit": {"2023-12-31": 100.0, "2024-12-31": 150.0},
        },
        "balance_sheet": {
            "Total Assets": {"2023-12-31": 500.0, "2024-12-31": 520.0},
            "Inventory": {"2023-12-31": 30.0, "2024-12-31": 45.0},
            "Accounts Receivable": {"2023-12-31": 20.0, "2024-12-31": 40.0},
            "Goodwill": {"2023-12-31": 60.0, "2024-12-31": 60.0},
            "Cash And Cash Equivalents": {"2023-12-31": 120.0, "2024-12-31": 90.0},
        },
        "cash_flow": {
            "Stock Based Compensation": {"2023-12-31": 5.0, "2024-12-31": 8.0},
        },
    }
    history = build_financial_history(payload)
    latest = history.annual[-1]
    assert latest.revenue == 300.0
    assert latest.gross_profit == 150.0
    assert latest.total_assets == 520.0
    assert latest.inventory == 45.0
    assert latest.accounts_receivable == 40.0
    assert latest.goodwill == 60.0
    assert latest.stock_based_compensation == 8.0

    # A payload missing every Phase 4 row (v4's existing behaviour) still
    # yields the pre-existing fields unchanged and None for the new ones.
    minimal = {
        "income_stmt": {"Total Revenue": {"2023-12-31": 10.0, "2024-12-31": 12.0}},
    }
    minimal_history = build_financial_history(minimal)
    assert minimal_history.annual[-1].revenue == 12.0
    assert minimal_history.annual[-1].total_assets is None
    assert minimal_history.annual[-1].inventory is None
    assert minimal_history.annual[-1].accounts_receivable is None
    assert minimal_history.annual[-1].goodwill is None
    assert minimal_history.annual[-1].stock_based_compensation is None


# ---------------------------------------------------------------------------
# incremental_roic: duration_multiplier only shortens, only when growth is
# elevated and ROIC sits below the config hurdle; corner cases stay None.
# ---------------------------------------------------------------------------

def test_incremental_roic_shortens_duration_only_for_high_growth_low_roic():
    config = load_model_v5_config()
    low_roic_signal = _signal("incremental_roic", 0.30, evidence={})  # large shortfall below 0.10 hurdle
    high_growth = apply_quality_features(_result(initial=0.30), _features(low_roic_signal), config=config)
    no_growth = apply_quality_features(_result(initial=0.0), _features(low_roic_signal), config=config)
    assert high_growth.duration_multiplier < 1.0
    assert no_growth.duration_multiplier == pytest.approx(1.0)


def test_incremental_roic_actually_moves_the_growth_mean_multiplier():
    """Audit fix 1 (2026-09-03): duration shortening must reach the actual
    return path, not just the display state and the ablation state_shift.
    The real 2026-09-02 run showed 214/214 incremental_roic ablations with
    zero P(target)/expected_cagr impact because duration_multiplier never
    fed build_scenarios. mean_multiplier must now differ from 1.0 whenever
    the signal has a real effect, composed with growth's own path."""
    from autoscreener.scoring.v5.growth import GrowthUpdate

    config = load_model_v5_config()
    low_roic_signal = _signal("incremental_roic", 0.30, evidence={})
    growth_update = GrowthUpdate(
        baseline_initial_rate=0.30, updated_initial_rate=0.30, terminal_rate=0.04,
        baseline_duration_years=2.4, updated_duration_years=2.4,
        baseline_fade=0.75, updated_fade=0.75, revenue_multiple_ratio=1.0,
        applied_keys=(), signal_effects={},
    )
    update = apply_quality_features(
        _result(initial=0.30), _features(low_roic_signal), config=config,
        growth_update=growth_update,
    )
    assert update.applied_keys == ("incremental_roic",)
    assert update.mean_multiplier < 1.0
    assert update.no_effect_keys == ()


def test_incremental_roic_zero_growth_is_reported_as_no_change_not_applied():
    """Audit fix 1: reduction_years == 0 (non-positive growth) must not be
    counted as "applied" -- it never moves anything (108/227 real cases in
    the 2026-09-02 run were exactly this)."""
    config = load_model_v5_config()
    low_roic_signal = _signal("incremental_roic", 0.30, evidence={})
    no_growth = apply_quality_features(
        _result(initial=0.0), _features(low_roic_signal), config=config,
    )
    assert no_growth.applied_keys == ()
    assert no_growth.no_effect_keys == ("incremental_roic",)
    assert no_growth.mean_multiplier == pytest.approx(1.0)


def test_incremental_roic_never_increases_mean_multiplier_when_initial_below_terminal():
    """Audit fix 2 (2026-09-03): when initial_rate < terminal_rate,
    accelerating fade toward the terminal rate raises the path instead of
    shortening it (22/105 real 2026-09-02 ablations showed a positive
    DeltaP(target) from a signal meant to be a penalty). The incremental
    ratio must be clamped to <= 1.0 regardless of which direction the
    recomposed path moves. Minor fix (2026-09-03 second audit): a ratio
    clamped all the way to 1.0 is a genuine no-op, not merely a
    bounded-but-nonzero effect -- it must be reported as no_effect, not
    "applied", matching the reduction_years <= 0 case."""
    from autoscreener.scoring.v5.growth import GrowthUpdate

    config = load_model_v5_config()
    low_roic_signal = _signal("incremental_roic", 0.30, evidence={})
    growth_update = GrowthUpdate(
        baseline_initial_rate=0.10, updated_initial_rate=0.10, terminal_rate=0.50,
        baseline_duration_years=2.4, updated_duration_years=2.4,
        baseline_fade=0.75, updated_fade=0.75, revenue_multiple_ratio=1.0,
        applied_keys=(), signal_effects={},
    )
    update = apply_quality_features(
        _result(initial=0.10, terminal=0.50), _features(low_roic_signal), config=config,
        growth_update=growth_update,
    )
    assert update.applied_keys == ()
    assert update.no_effect_keys == ("incremental_roic",)
    assert update.mean_multiplier == pytest.approx(1.0)
    assert (
        update.signal_effects["incremental_roic"]["revenue_multiple_ratio_from_duration"]
        <= 1.0 + 1e-12
    )


def test_incremental_roic_above_hurdle_never_extends_duration():
    config = load_model_v5_config()
    # value is the shortfall below the hurdle rate; 0.0 = at/above hurdle,
    # which must never produce a duration *extension* (only shrinkage is
    # possible, and only when the shortfall is positive).
    at_hurdle = _signal("incremental_roic", 0.0)
    update = apply_quality_features(_result(initial=0.30), _features(at_hurdle), config=config)
    assert update.duration_multiplier == pytest.approx(1.0)
    empty = _features()
    baseline = apply_quality_features(_result(initial=0.30), empty, config=config)
    assert baseline.duration_multiplier == pytest.approx(1.0)


def test_incremental_roic_none_when_delta_ic_non_positive_or_nopat_missing():
    config = load_model_v5_config()
    start = _period(datetime.date(2022, 12, 31), operating_income=10.0, total_debt=50.0, cash_and_equivalents=10.0)
    # Same invested capital both periods -> delta_ic == 0 -> incremental_roic is None.
    end_same_ic = _period(datetime.date(2024, 12, 31), operating_income=30.0, total_debt=50.0, cash_and_equivalents=10.0)
    items = [V5PitInput(
        ticker_id=1, symbol="ZZ", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(start, end_same_ic),
    )]
    with session_scope() as session:
        feature_sets = build_quality_feature_sets(session, items, as_of=datetime.date(2024, 12, 31), config=config)
    signal = next(s for s in feature_sets[1].signals if s.key == "incremental_roic")
    assert signal.value is None
    assert signal.applied is False

    # Operating income non-positive in the end period -> NOPAT proxy is None
    # -> delta_nopat is None -> incremental_roic stays None (never "bad = 0").
    end_loss = _period(
        datetime.date(2024, 12, 31), operating_income=-5.0, total_debt=90.0, cash_and_equivalents=10.0
    )
    items2 = [V5PitInput(
        ticker_id=2, symbol="ZZ2", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(start, end_loss),
    )]
    with session_scope() as session:
        feature_sets2 = build_quality_feature_sets(session, items2, as_of=datetime.date(2024, 12, 31), config=config)
    signal2 = next(s for s in feature_sets2[2].signals if s.key == "incremental_roic")
    assert signal2.value is None
    assert signal2.status in {"insufficient_annual_history", "delta_ic_non_positive_or_nopat_unavailable",
                               "runtime_disabled_low_coverage"}


def test_per_share_economics_none_when_shares_outstanding_missing():
    config = load_model_v5_config()
    start = _period(
        datetime.date(2022, 12, 31), revenue=100.0, gross_profit=40.0, free_cash_flow=10.0,
        shares_outstanding=None,
    )
    end = _period(
        datetime.date(2024, 12, 31), revenue=200.0, gross_profit=90.0, free_cash_flow=25.0,
        shares_outstanding=None,
    )
    items = [V5PitInput(
        ticker_id=3, symbol="ZZ3", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(start, end),
    )]
    with session_scope() as session:
        feature_sets = build_quality_feature_sets(session, items, as_of=datetime.date(2024, 12, 31), config=config)
    signal = next(s for s in feature_sets[3].signals if s.key == "per_share_economics")
    # Never falls back to the whole-company CAGR when shares are missing.
    assert signal.value is None


# ---------------------------------------------------------------------------
# cash_conversion: divide-by-near-zero guard and winsorization.
# ---------------------------------------------------------------------------

def test_cash_conversion_guards_near_zero_net_income_and_winsorizes():
    config = load_model_v5_config()
    tiny_ni = _period(
        datetime.date(2024, 12, 31), revenue=1000.0, net_income=0.01,
        operating_cash_flow=500.0, free_cash_flow=400.0,
    )
    items = [V5PitInput(
        ticker_id=4, symbol="ZZ4", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(tiny_ni,),
    )]
    with session_scope() as session:
        feature_sets = build_quality_feature_sets(session, items, as_of=datetime.date(2024, 12, 31), config=config)
    signal = next(s for s in feature_sets[4].signals if s.key == "cash_conversion")
    assert signal.value is None
    assert signal.status == "net_income_near_zero"

    extreme = _period(
        # net_income is 1.5% of revenue (above the 1% floor) but OCF/NI is
        # a wildly implausible 33x -- must be winsorized, not passed through.
        datetime.date(2024, 12, 31), revenue=1000.0, net_income=15.0,
        operating_cash_flow=500.0, free_cash_flow=400.0,
    )
    items2 = [V5PitInput(
        ticker_id=5, symbol="ZZ5", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(extreme,),
    )]
    with session_scope() as session:
        feature_sets2 = build_quality_feature_sets(session, items2, as_of=datetime.date(2024, 12, 31), config=config)
    signal2 = next(s for s in feature_sets2[5].signals if s.key == "cash_conversion")
    assert signal2.value == pytest.approx(config.quality.cash_conversion_ratio_winsor_abs)


# ---------------------------------------------------------------------------
# accounting_quality: widens sigma/left tail only, never lowers the
# conditional mean (Issue #3 section 6.3).
# ---------------------------------------------------------------------------

def test_accounting_quality_widens_sigma_and_left_tail_not_conditional_mean():
    config = load_model_v5_config()
    result = _seed_result()
    baseline = build_scenarios(result, confidence=0.5, config=config)
    widened = build_scenarios(
        result, confidence=0.5, config=config,
        sigma_multiplier=config.quality.accounting_sigma_max_multiplier,
        left_tail_extra=config.quality.accounting_left_tail_extra_max,
    )
    baseline_mean = sum(s.weight * s.conditional_expected_moic for s in baseline)
    widened_mean = sum(s.weight * s.conditional_expected_moic for s in widened)
    assert widened_mean == pytest.approx(baseline_mean)
    for name, base_s, wide_s in zip(("downside", "base", "upside"), baseline, widened):
        assert wide_s.conditional_expected_moic == pytest.approx(base_s.conditional_expected_moic)
        assert wide_s.log_sigma >= base_s.log_sigma
    downside_widened = widened[0].log_sigma / baseline[0].log_sigma
    base_widened = widened[1].log_sigma / baseline[1].log_sigma
    assert downside_widened > base_widened  # left tail widened strictly more


def test_accounting_severity_only_counts_computable_checks():
    config = load_model_v5_config()
    prior = _period(
        datetime.date(2023, 12, 31), revenue=100.0, net_income=10.0,
        operating_cash_flow=2.0,  # weak cash conversion in the latest period
        accounts_receivable=10.0, inventory=None, total_assets=None, goodwill=None,
        stock_based_compensation=None,
    )
    latest = _period(
        datetime.date(2024, 12, 31), revenue=120.0, net_income=12.0,
        operating_cash_flow=3.0,
        accounts_receivable=15.0, inventory=None, total_assets=None, goodwill=None,
        stock_based_compensation=None,
    )
    items = [V5PitInput(
        ticker_id=6, symbol="ZZ6", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(prior, latest),
    )]
    with session_scope() as session:
        feature_sets = build_quality_feature_sets(session, items, as_of=datetime.date(2024, 12, 31), config=config)
    signal = next(s for s in feature_sets[6].signals if s.key == "accounting_quality")
    assert signal.value is not None
    assert 0.0 < signal.value <= 1.0
    assert signal.evidence["sbc_to_revenue"] is None
    assert signal.evidence["goodwill_to_assets"] is None


# ---------------------------------------------------------------------------
# Missingness never moves state; only lowers confidence (same contract as
# growth.py). Coverage gate disables a feature universe-wide even for a
# ticker that has a row.
# ---------------------------------------------------------------------------

def test_missingness_is_neutral_to_state_but_lowers_confidence_separately():
    missing = _signal(
        "accounting_quality", None, applied=False, coverage=CoverageStatus.NOT_COLLECTED,
    )
    failed = _signal(
        "reconciliation_confidence", None, applied=False, coverage=CoverageStatus.COLLECTION_FAILED,
    )
    features = _features(missing, failed)
    config = load_model_v5_config()
    update = apply_quality_features(_result(), features, config=config)
    assert update.duration_multiplier == pytest.approx(1.0)
    assert update.mean_multiplier == pytest.approx(1.0)
    assert update.sigma_multiplier == pytest.approx(1.0)
    assert update.left_tail_extra == pytest.approx(0.0)
    assert features.confidence_delta < 0


def test_low_coverage_runtime_gate_disables_feature_even_with_a_row():
    config = load_model_v5_config()
    good_period = _period(
        datetime.date(2023, 12, 31), operating_income=10.0, total_debt=50.0, cash_and_equivalents=10.0,
    )
    good_period2 = _period(
        datetime.date(2024, 12, 31), operating_income=30.0, total_debt=90.0, cash_and_equivalents=10.0,
    )
    has_data = V5PitInput(
        ticker_id=7, symbol="ZZ7", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
        raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
        input_status="collected_with_data", financial_annual=(good_period, good_period2),
    )
    # A large population of tickers with zero financial history keeps universe
    # coverage for incremental_roic below the configured threshold even
    # though ticker 7 itself has a perfectly usable row.
    no_data = [
        V5PitInput(
            ticker_id=100 + i, symbol=f"ZZN{i}", as_of=datetime.date(2024, 12, 31), moic_inputs=None,
            raw_snapshot_id=None, raw_available_from=None, price_as_of=None,
            input_status="not_collected", financial_annual=(),
        )
        for i in range(20)
    ]
    with session_scope() as session:
        feature_sets = build_quality_feature_sets(
            session, [has_data, *no_data], as_of=datetime.date(2024, 12, 31), config=config
        )
    signal = next(s for s in feature_sets[7].signals if s.key == "incremental_roic")
    assert signal.coverage_status == CoverageStatus.COLLECTED_WITH_DATA
    assert signal.status == "runtime_disabled_low_coverage"
    assert signal.applied is False
    assert feature_sets[7].universe_coverage["incremental_roic"] < 0.60


# ---------------------------------------------------------------------------
# PIT: XbrlFact filed after as_of, and FinancialPeriod ends after as_of, must
# never be read.
# ---------------------------------------------------------------------------

def test_reconciliation_only_reads_facts_filed_on_or_before_as_of():
    symbol = "ZZV5RECON"
    as_of = datetime.date(2024, 6, 30)
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.query(XbrlFact).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(XbrlFact(
            ticker_id=ticker_id, taxonomy="us-gaap",
            tag="RevenueFromContractWithCustomerExcludingAssessedTax", unit="USD",
            period_start=datetime.date(2023, 1, 1), period_end=datetime.date(2023, 12, 31),
            value=100.0, form="10-K", accession_number="visible-past",
            filed_date=datetime.date(2024, 2, 1),
        ))
        session.add(XbrlFact(
            ticker_id=ticker_id, taxonomy="us-gaap",
            tag="RevenueFromContractWithCustomerExcludingAssessedTax", unit="USD",
            period_start=datetime.date(2024, 1, 1), period_end=datetime.date(2024, 12, 31),
            value=999.0, form="10-K", accession_number="future-not-visible",
            filed_date=datetime.date(2024, 8, 1),  # after as_of -- must be invisible
        ))
    latest = _period(datetime.date(2023, 12, 31), revenue=100.0)
    item = V5PitInput(
        ticker_id=ticker_id, symbol=symbol, as_of=as_of, moic_inputs=None,
        raw_snapshot_id=1, raw_available_from=as_of, price_as_of=as_of,
        input_status="collected_with_data", financial_annual=(latest,),
    )
    try:
        with session_scope() as session:
            feature_sets = build_quality_feature_sets(session, [item], as_of=as_of, config=load_model_v5_config())
        signal = next(s for s in feature_sets[ticker_id].signals if s.key == "reconciliation_confidence")
        assert signal.evidence["comparable_concepts"] == 1
        revenue_item = signal.evidence["items"][0]
        assert revenue_item["status"] == "match"
        assert revenue_item["sec_filed_date"] == "2024-02-01"
    finally:
        with session_scope() as session:
            session.query(XbrlFact).filter_by(ticker_id=ticker_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()


def test_build_v5_pit_inputs_never_includes_financial_periods_after_as_of():
    symbol = "ZZV5FINPIT"
    as_of = datetime.date(2024, 6, 30)
    payload = {
        "info": {"currency": "USD", "financialCurrency": "USD"},
        "income_stmt": {
            "Total Revenue": {
                "2022-12-31": 100.0, "2023-12-31": 150.0,
                # This period ends after ``as_of`` and must never be visible,
                # even though the raw_snapshot row itself is available_from <=
                # as_of (a stale/incorrectly-dated payload should not leak a
                # future period into the model).
                "2025-12-31": 999.0,
            },
        },
    }
    with session_scope() as session:
        old = session.query(Ticker).filter_by(symbol=symbol).one_or_none()
        if old is not None:
            session.query(RawSnapshot).filter_by(ticker_id=old.id).delete()
            session.query(UniverseSnapshot).filter_by(ticker_id=old.id).delete()
            session.delete(old)
        ticker = Ticker(symbol=symbol, market="US")
        session.add(ticker)
        session.flush()
        ticker_id = ticker.id
        session.add(RawSnapshot(
            ticker_id=ticker_id, snapshot_date=as_of, source="test", payload=payload,
            content_hash="phase4-financial-pit", last_seen_date=as_of, available_from=as_of,
        ))
        session.add(UniverseSnapshot(snapshot_date=as_of, ticker_id=ticker_id, included=True))
    try:
        with session_scope() as session:
            items = build_v5_pit_inputs(session, as_of=as_of)
        item = next(i for i in items if i.ticker_id == ticker_id)
        assert [p.revenue for p in item.financial_annual] == [100.0, 150.0]
        assert all(period.period_end <= as_of for period in item.financial_annual)
    finally:
        with session_scope() as session:
            session.query(RawSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(UniverseSnapshot).filter_by(ticker_id=ticker_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()


# ---------------------------------------------------------------------------
# Applied features always carry a computed ablation; unapplied ones carry an
# explicit not_computed reason (never a fabricated zero impact).
# ---------------------------------------------------------------------------

def test_default_quality_config_reproduces_phase2_phase3_distribution_exactly():
    """Empty/no-op quality features must not perturb Phase 2/3 numbers."""
    config = load_model_v5_config()
    result = _seed_result()
    baseline = build_scenarios(result, confidence=0.5, config=config)
    empty_update = apply_quality_features(_result(), _features(), config=config)
    assert empty_update.duration_multiplier == pytest.approx(1.0)
    assert empty_update.mean_multiplier == pytest.approx(1.0)
    assert empty_update.sigma_multiplier == pytest.approx(1.0)
    assert empty_update.left_tail_extra == pytest.approx(0.0)
    assert empty_update.applied_keys == ()
    no_op = build_scenarios(
        result, confidence=0.5, config=config,
        conditional_mean_multiplier=1.0,
        sigma_multiplier=empty_update.sigma_multiplier,
        left_tail_extra=empty_update.left_tail_extra,
    )
    for base_s, noop_s in zip(baseline, no_op):
        assert noop_s.log_mu == pytest.approx(base_s.log_mu)
        assert noop_s.log_sigma == pytest.approx(base_s.log_sigma)


def test_shadow_run_persists_quality_ablation_without_touching_v4(monkeypatch):
    # WP-A2(docs/racr_wp_a2_test_fixture_repair_2026-09-04.md):以前は
    # 「DBに既にTickerが1件ある」前提で `.first()` を読んでいたが、隔離済み
    # テストDBは0件から始まるため `None` を返し `.id` で落ちていた。V5 shadow
    # runの永続化を確認するだけなので、対象Tickerは自前で1件作れば十分。
    as_of = datetime.date(2024, 6, 30)
    symbol = "ZZV5QUALSHADOW"
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
    quality_set = _features(
        _signal("accounting_quality", 0.5, evidence={"severity": 0.5, "warnings": ["weak_cash_conversion"]}),
        _signal("incremental_roic", None, applied=False, coverage=CoverageStatus.NOT_COLLECTED),
        _signal("per_share_economics", None, applied=False, coverage=CoverageStatus.NOT_COLLECTED),
        _signal("cash_conversion", None, applied=False, coverage=CoverageStatus.NOT_COLLECTED),
        _signal("reconciliation_confidence", None, applied=False, coverage=CoverageStatus.NOT_COLLECTED),
    )
    empty_growth = GrowthFeatureSet((), {})
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
        lambda *a, **k: {ticker_id: quality_set},
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
            impact = score.features["ablation"]["accounting_quality"]
            assert score.states["contract_version"] == "v5.phase6"
            assert score.states["state_updates_applied"] == ["accounting_quality"]
            assert score.states["economics"]["reinvestment_efficiency"]["status"] == "not_collected"
            assert impact["status"] == "computed"
            assert impact["state_shift"]["sigma_multiplier"] > 0
            # Every Phase 4 key not applied still carries an explicit reason,
            # never a fabricated zero impact.
            for key in ("incremental_roic", "per_share_economics", "cash_conversion",
                        "reconciliation_confidence"):
                entry = score.features["ablation"][key]
                assert entry["status"] == "not_computed"
                assert entry["reason"]
            assert run.metrics["applied_feature_counts"] == {"accounting_quality": 1}
            assert run.metrics["ablation_results"] == 1
            assert session.query(Score).count() == v4_before
    finally:
        with session_scope() as session:
            session.query(ModelRun).filter_by(id=run_id).delete()
            session.query(Ticker).filter_by(id=ticker_id).delete()
