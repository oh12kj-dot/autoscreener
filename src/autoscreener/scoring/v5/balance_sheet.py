"""Phase 5/6 debt-maturity, liquidity, capital-allocation, and future-
dilution-capacity updates.

Mirrors the ``quality.py``/``growth.py`` API by design: every builder is
point-in-time, coverage-gated, and returns explicit missingness.

``debt_maturity``/``liquidity``/``capital_allocation`` shrink
``survival_probability`` (never touched before Phase 5 -- "Survival is held
constant until Phase 5/6", scenario.py's old docstring). None can ever raise
survival above the v4-seeded baseline; a well-covered maturity wall, ample
liquidity, or no aggressive capital-return commitment all get a no-op
multiplier of 1.0, matching the same never-reward-merely-having-data
convention as every earlier phase.

``future_dilution_capacity`` (added 2026-09-03, Phase 6, Issue #3 section
12) reads ``dilution_capacity`` (ATM/shelf remaining authorization,
unexercised options/warrants, variable-conversion flag) and decays the
growth mean multiplier -- future diluted share count -> per-share value.
This required a user decision: ``collect_dilution.py``'s docstring states,
as an explicit numbered principle, that table is never read by
``evaluate_gates`` or ``scoring/`` (and ``scoring/v5/`` is a subpackage of
``scoring/``). The Phase 5 doc recorded this conflict rather than guessing;
the user's decision (2026-09-03) was to interpret that principle as scoped
to v4's ``evaluate_gates``/``scoring/`` specifically -- v4's own behavior is
unchanged -- and to let v5, an independent shadow challenger, read it. See
``collect_dilution.py``'s docstring for the same decision recorded at the
source, and docs/model_v5_phase6_tail_macro_competing_risk_2026-09-03.md for
the full triple-counting analysis against v4's ``dilution_drag`` and Phase
4's ``per_share_economics``.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, replace

from sqlalchemy.orm import Session

from autoscreener.config import ModelV5Config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import (
    CapitalAllocationEvent,
    DebtInstrument,
    DilutionCapacity,
    LiquidityFacility,
    LiveDatasetCoverage,
)
from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.growth import _coverage_status, _cutoff, _reliability
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.quality import QualityUpdate
from autoscreener.scoring.v5.reliability import decayed_reliability, feature_confidence_delta

_FEATURE_KEYS = ("debt_maturity", "liquidity", "capital_allocation", "future_dilution_capacity")
_LEDGER_DATASET = {
    "debt_maturity": "debt_profile", "liquidity": "debt_profile",
    "capital_allocation": "capital_allocation",
}
# routes.py:3597's exact trailing-window definition for capital-allocation
# totals, reused rather than re-derived.
_OUTFLOW_TYPES = frozenset({"buyback", "dividend"})
_INFLOW_TYPES = frozenset({"debt_raise", "equity_raise"})


@dataclass(frozen=True)
class CapitalSignal:
    key: str
    status: str
    coverage_status: str
    runtime_enabled: bool
    applied: bool
    reliability: float
    observed_at: datetime.datetime | None
    value: float | None
    evidence: dict

    def to_dict(self) -> dict:
        payload = asdict(self)
        if self.observed_at is not None:
            payload["observed_at"] = self.observed_at.isoformat()
        return payload


@dataclass(frozen=True)
class CapitalFeatureSet:
    signals: tuple[CapitalSignal, ...]
    universe_coverage: dict[str, float]

    @property
    def applied_keys(self) -> tuple[str, ...]:
        return tuple(signal.key for signal in self.signals if signal.applied)

    @property
    def confidence_delta(self) -> float:
        # WP-D D-2 (docs/racr_wp_d_reliability_layer_2026-09-04.md): shared
        # contract from reliability.py -- see growth.py's identical property.
        return feature_confidence_delta(self.signals)

    def to_dict(self) -> dict:
        return {
            "universe_coverage": self.universe_coverage,
            "signals": [signal.to_dict() for signal in self.signals],
            "applied_keys": list(self.applied_keys),
            "confidence_delta": self.confidence_delta,
        }

    def excluding(self, key: str) -> CapitalFeatureSet:
        return CapitalFeatureSet(
            tuple(
                replace(signal, applied=False, status="ablated")
                if signal.key == key else signal
                for signal in self.signals
            ),
            dict(self.universe_coverage),
        )


@dataclass(frozen=True)
class CapitalUpdate:
    survival_multiplier: float
    mean_multiplier: float
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]
    # Signals whose feature-level candidacy (nonzero raw value) resolved to
    # zero real effect once the actual result/quality budget was known --
    # e.g. future_dilution_capacity when the shared anti-triple-counting
    # budget (max_combined_dilution_reduction) was already exhausted by
    # Phase 4's per_share_economics/incremental_roic. Same discipline as
    # quality.py's QualityUpdate.no_effect_keys (2026-09-03 audit).
    no_effect_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_signal(key: str, coverage_status: str, status: str | None = None) -> CapitalSignal:
    return CapitalSignal(
        key=key, status=status or coverage_status, coverage_status=coverage_status,
        runtime_enabled=False, applied=False, reliability=0.0,
        observed_at=None, value=None, evidence={},
    )


def _debt_maturity_signal(
    debt_rows: list[DebtInstrument], liquidity_row: LiquidityFacility | None,
    *, as_of: datetime.date,
) -> CapitalSignal:
    status = _coverage_status(debt_rows)
    if liquidity_row is None:
        return _empty_signal("debt_maturity", status, "no_liquidity_facility_data")
    cash = float(liquidity_row.cash_balance) if liquidity_row.cash_balance is not None else None
    revolver = (
        float(liquidity_row.revolver_available) if liquidity_row.revolver_available is not None else None
    )
    if cash is None:
        return _empty_signal("debt_maturity", status, "cash_balance_unavailable")
    if status != CoverageStatus.COLLECTED_WITH_DATA and status != CoverageStatus.COLLECTED_NO_FINDING:
        # debt_instruments itself was never scanned for this ticker even
        # though a liquidity_facilities row exists; an empty maturity ladder
        # cannot be trusted as "no debt" without that scan having happened.
        return _empty_signal("debt_maturity", status, "debt_instruments_not_scanned")
    available = cash + (revolver or 0.0)
    # routes.py:3619's exact definition, reused: no lower bound on
    # maturity_date, so already-past-due principal counts too (more urgent,
    # not less).
    due_12m = sum(
        float(row.principal) for row in debt_rows
        if row.principal is not None and row.maturity_date is not None
        and row.maturity_date <= as_of + datetime.timedelta(days=365)
    )
    if due_12m <= 0:
        shortfall = 0.0
    elif available <= 0:
        # Fully uncovered. A bounded sentinel, not an unbounded ratio -- an
        # unbounded value here would repeat the float("inf")-in-JSONB defect
        # Phase 4's reconciliation_confidence signal hit (2026-09-03 audit).
        shortfall = 1.0
    else:
        shortfall = max(0.0, due_12m / available - 1.0)
    observed_at = max(
        (row.observed_at for row in debt_rows), default=liquidity_row.observed_at,
    )
    evidence = {
        "due_within_12m": due_12m, "cash_balance": cash, "revolver_available": revolver,
        "available_liquidity": available,
        "coverage_ratio": (available / due_12m) if due_12m > 0 else None,
        "instrument_count": len(debt_rows),
    }
    reliability = min(
        _reliability(row.confidence) for row in (*debt_rows, liquidity_row)
    ) if debt_rows else _reliability(liquidity_row.confidence)
    return CapitalSignal(
        "debt_maturity", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        reliability, observed_at, shortfall, evidence,
    )


def _liquidity_signal(
    liquidity_row: LiquidityFacility | None, latest_fcf: float | None,
    *, config: ModelV5Config,
) -> CapitalSignal:
    if liquidity_row is None:
        return _empty_signal("liquidity", CoverageStatus.NOT_COLLECTED, "no_liquidity_facility_data")
    cash = float(liquidity_row.cash_balance) if liquidity_row.cash_balance is not None else None
    if cash is None:
        return _empty_signal(
            "liquidity", CoverageStatus.COLLECTED_WITH_DATA, "cash_balance_unavailable"
        )
    if latest_fcf is None:
        return _empty_signal(
            "liquidity", CoverageStatus.COLLECTED_WITH_DATA, "fcf_unavailable"
        )
    if latest_fcf >= 0:
        # Not burning cash: no bonus for a long/infinite runway, matching
        # the no-reward-for-merely-having-data convention.
        shortfall = 0.0
        runway_months = None
    else:
        monthly_burn = -latest_fcf / 12.0
        runway_months = cash / monthly_burn if monthly_burn > 0 else None
        if runway_months is None:
            shortfall = 0.0
        else:
            shortfall = max(0.0, config.capital.liquidity_runway_floor_months - runway_months)
    evidence = {
        "cash_balance": cash, "latest_fcf": latest_fcf, "runway_months": runway_months,
        "runway_floor_months": config.capital.liquidity_runway_floor_months,
    }
    return CapitalSignal(
        "liquidity", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _reliability(liquidity_row.confidence), liquidity_row.observed_at, shortfall, evidence,
    )


def _capital_allocation_signal(
    events: list[CapitalAllocationEvent], liquidity_row: LiquidityFacility | None,
    *, as_of: datetime.date, config: ModelV5Config,
) -> CapitalSignal:
    status = _coverage_status(events)
    if liquidity_row is None or liquidity_row.cash_balance is None:
        return _empty_signal("capital_allocation", status, "no_cash_balance_to_compare_against")
    cash = float(liquidity_row.cash_balance)
    lookback = datetime.timedelta(days=config.capital.capital_allocation_lookback_days)
    window_start = datetime.datetime.combine(
        as_of, datetime.time.min, tzinfo=datetime.timezone.utc
    ) - lookback
    recent = [
        row for row in events
        if row.announced_at >= window_start
        and str(row.coverage_status) == CoverageStatus.COLLECTED_WITH_DATA
        and row.amount is not None
    ]
    if not recent and status != CoverageStatus.COLLECTED_WITH_DATA:
        return _empty_signal("capital_allocation", status, "no_events_in_lookback_window")
    outflow = sum(float(row.amount) for row in recent if row.event_type in _OUTFLOW_TYPES)
    inflow = sum(float(row.amount) for row in recent if row.event_type in _INFLOW_TYPES)
    net_commitment = outflow - inflow
    if net_commitment <= 0 or cash <= 0:
        # A net capital raiser (or a company reporting events but with no
        # cash figure to compare against) gets no penalty here -- raising
        # capital is not itself treated as bad news; only a committed net
        # cash return large relative to the cash balance is a liquidity
        # stress signal.
        shortfall = 0.0
    else:
        shortfall = max(0.0, net_commitment / cash)
    observed_at = max((row.observed_at for row in recent), default=None)
    evidence = {
        "lookback_days": config.capital.capital_allocation_lookback_days,
        "outflow_buyback_dividend": outflow, "inflow_debt_equity_raise": inflow,
        "net_commitment": net_commitment, "cash_balance": cash,
        "event_count_in_window": len(recent),
    }
    reliability = (
        min(_reliability(row.confidence) for row in recent) if recent
        else _reliability(liquidity_row.confidence)
    )
    return CapitalSignal(
        "capital_allocation", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        reliability, observed_at, shortfall, evidence,
    )


def _future_dilution_capacity_signal(
    rows: list[DilutionCapacity], market_cap: float | None, *, config: ModelV5Config,
) -> CapitalSignal:
    """ATM/shelf remaining authorization + unexercised options/warrants +
    a variable-conversion flag -- unissued future capacity, structurally
    distinct from v4's ``dilution_drag`` and Phase 4's
    ``per_share_economics`` (both derived from *realized*, historical share
    counts). See ``apply_capital_features`` for the explicit anti-triple-
    counting budget shared with those two.

    ``dilution_capacity`` has no coverage ledger and no per-row
    ``coverage_status``/``confidence`` column (unlike the K-1 Live
    Intelligence tables) -- ``collect_dilution.py`` only upserts a row when
    it found *something* (``if not evidence: return False``), so an absent
    row cannot be distinguished from "never scanned" here. Treated
    conservatively as NOT_COLLECTED either way, same as growth.py's base
    signals before any ledger fallback.
    """
    if not rows:
        return _empty_signal(
            "future_dilution_capacity", CoverageStatus.NOT_COLLECTED,
            "no_dilution_capacity_disclosure_found",
        )
    latest = max(rows, key=lambda row: row.collected_on)
    atm = float(latest.atm_remaining_usd) if latest.atm_remaining_usd is not None else None
    shelf = float(latest.shelf_remaining_usd) if latest.shelf_remaining_usd is not None else None
    equity_capacity_ratio = None
    if (atm is not None or shelf is not None) and market_cap is not None and market_cap > 0:
        equity_capacity_ratio = ((atm or 0.0) + (shelf or 0.0)) / market_cap
    options_ratio = (
        float(latest.unexercised_options_ratio)
        if latest.unexercised_options_ratio is not None else None
    )
    has_variable_conversion = bool(latest.has_variable_conversion)
    if equity_capacity_ratio is None and options_ratio is None and not has_variable_conversion:
        return _empty_signal(
            "future_dilution_capacity", CoverageStatus.COLLECTED_WITH_DATA,
            "missing_required_fields",
        )
    overhang = 0.0
    if equity_capacity_ratio is not None:
        overhang += min(
            config.capital.future_dilution_atm_shelf_component_cap, max(0.0, equity_capacity_ratio)
        )
    if options_ratio is not None:
        overhang += min(
            config.capital.future_dilution_options_component_cap, max(0.0, options_ratio)
        )
    if has_variable_conversion:
        overhang += config.capital.future_dilution_variable_conversion_bump
    evidence = {
        "atm_remaining_usd": atm, "shelf_remaining_usd": shelf, "market_cap": market_cap,
        "equity_capacity_ratio": equity_capacity_ratio, "unexercised_options_ratio": options_ratio,
        "has_variable_conversion": has_variable_conversion,
        "collected_on": latest.collected_on.isoformat(),
    }
    observed_at = datetime.datetime.combine(
        latest.collected_on, datetime.time.min, tzinfo=datetime.timezone.utc
    )
    # No per-row confidence field exists on this table (K-4 extraction mixes
    # XBRL options data with S-3/424B5/10-Q text regex); a flat, moderate
    # reliability documents that rather than fabricating a per-row tier.
    return CapitalSignal(
        "future_dilution_capacity", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        0.60, observed_at, overhang, evidence,
    )


def build_capital_feature_sets(
    session: Session,
    items: list[V5PitInput],
    *,
    as_of: datetime.date,
    config: ModelV5Config,
) -> dict[int, CapitalFeatureSet]:
    """Load Phase 5 datasets in bulk under the same end-of-day PIT boundary."""
    ticker_ids = [item.ticker_id for item in items]
    if not ticker_ids:
        return {}
    cutoff = _cutoff(as_of)
    debt_rows = session.query(DebtInstrument).filter(
        DebtInstrument.ticker_id.in_(ticker_ids), DebtInstrument.observed_at < cutoff,
    ).order_by(DebtInstrument.observed_at, DebtInstrument.id).all()
    liquidity_rows = session.query(LiquidityFacility).filter(
        LiquidityFacility.ticker_id.in_(ticker_ids), LiquidityFacility.observed_at < cutoff,
    ).order_by(LiquidityFacility.observed_at, LiquidityFacility.id).all()
    event_rows = session.query(CapitalAllocationEvent).filter(
        CapitalAllocationEvent.ticker_id.in_(ticker_ids),
        CapitalAllocationEvent.observed_at < cutoff,
        CapitalAllocationEvent.announced_at < cutoff,
    ).order_by(CapitalAllocationEvent.announced_at, CapitalAllocationEvent.id).all()
    # DilutionCapacity has no observed_at (only collected_on, a Date) and no
    # coverage ledger -- PIT-filtered on collected_on <= as_of directly,
    # same convention as XbrlFact.filed_date.
    dilution_rows = session.query(DilutionCapacity).filter(
        DilutionCapacity.ticker_id.in_(ticker_ids), DilutionCapacity.collected_on <= as_of,
    ).order_by(DilutionCapacity.collected_on, DilutionCapacity.id).all()
    ledger_rows = session.query(LiveDatasetCoverage).filter(
        LiveDatasetCoverage.ticker_id.in_(ticker_ids),
        LiveDatasetCoverage.dataset.in_(set(_LEDGER_DATASET.values())),
        LiveDatasetCoverage.observed_at < cutoff,
    ).order_by(LiveDatasetCoverage.observed_at, LiveDatasetCoverage.id).all()

    def group(rows):
        output: dict[int, list] = {ticker_id: [] for ticker_id in ticker_ids}
        for row in rows:
            output[row.ticker_id].append(row)
        return output

    debt_by = group(debt_rows)
    events_by = group(event_rows)
    dilution_by = group(dilution_rows)
    liquidity_by: dict[int, LiquidityFacility | None] = {ticker_id: None for ticker_id in ticker_ids}
    for row in liquidity_rows:
        liquidity_by[row.ticker_id] = row  # last (most recent observed_at) wins
    ledger_by: dict[tuple[int, str], LiveDatasetCoverage] = {}
    for row in ledger_rows:
        ledger_by[(row.ticker_id, row.dataset)] = row

    financial_by = {item.ticker_id: item for item in items}

    candidates: dict[int, dict[str, CapitalSignal]] = {}
    for ticker_id in ticker_ids:
        liquidity_row = liquidity_by[ticker_id]
        item = financial_by[ticker_id]
        latest_fcf = (
            item.financial_annual[-1].free_cash_flow if item.financial_annual else None
        )
        # getattr, not direct attribute access: several existing tests mock
        # moic_inputs with a partial SimpleNamespace, not the full MoicInputs
        # dataclass -- a missing attribute should degrade to "unavailable",
        # matching every other None-means-missing convention here, not crash.
        market_cap = getattr(item.moic_inputs, "market_cap", None) if item.moic_inputs is not None else None
        candidate = {
            "debt_maturity": _debt_maturity_signal(
                debt_by[ticker_id], liquidity_row, as_of=as_of
            ),
            "liquidity": _liquidity_signal(liquidity_row, latest_fcf, config=config),
            "capital_allocation": _capital_allocation_signal(
                events_by[ticker_id], liquidity_row, as_of=as_of, config=config
            ),
            "future_dilution_capacity": _future_dilution_capacity_signal(
                dilution_by[ticker_id], market_cap, config=config
            ),
        }
        for key, dataset in _LEDGER_DATASET.items():
            if candidate[key].coverage_status == CoverageStatus.NOT_COLLECTED:
                ledger = ledger_by.get((ticker_id, dataset))
                if ledger is not None:
                    candidate[key] = replace(
                        candidate[key], coverage_status=str(ledger.coverage_status),
                        status=str(ledger.coverage_status), observed_at=ledger.observed_at,
                        evidence={"coverage_ledger_id": ledger.id, "reason_code": ledger.reason_code},
                    )
        candidates[ticker_id] = candidate

    coverage = {
        key: sum(
            candidates[ticker_id][key].coverage_status == CoverageStatus.COLLECTED_WITH_DATA
            for ticker_id in ticker_ids
        ) / len(ticker_ids)
        for key in _FEATURE_KEYS
    }
    output: dict[int, CapitalFeatureSet] = {}
    for ticker_id in ticker_ids:
        signals = []
        for key in _FEATURE_KEYS:
            signal = candidates[ticker_id][key]
            spec = FEATURES_BY_KEY[key]
            configured = config.feature_flags.get(key, spec.default_enabled)
            runtime_enabled = configured and coverage[key] >= spec.required_coverage
            # WP-D D-3 (docs/racr_wp_d_reliability_layer_2026-09-04.md):
            # wires FeatureSpec.freshness_half_life_days (a no-op today --
            # no Phase 5/6 signal sets it -- but no longer dead metadata).
            effective_reliability = decayed_reliability(
                signal, half_life_days=spec.freshness_half_life_days, as_of=as_of,
            )
            if not configured:
                status = "disabled_by_config"
            elif not runtime_enabled:
                status = "runtime_disabled_low_coverage"
            elif signal.status != "candidate":
                status = signal.status
            elif effective_reliability < spec.min_reliability:
                status = "below_min_reliability"
            elif signal.value is None or abs(signal.value) < 1e-12:
                status = "no_change"
            else:
                status = "applied"
            signals.append(replace(
                signal, status=status, runtime_enabled=runtime_enabled,
                applied=status == "applied", reliability=effective_reliability,
                evidence={**signal.evidence, "universe_coverage": coverage[key],
                          "required_coverage": spec.required_coverage,
                          "freshness_half_life_days": spec.freshness_half_life_days},
            ))
        output[ticker_id] = CapitalFeatureSet(tuple(signals), dict(coverage))
    return output


def apply_capital_features(
    result: MoicResult,
    features: CapitalFeatureSet,
    *,
    config: ModelV5Config,
    quality_update: QualityUpdate | None = None,
    excluded_key: str | None = None,
) -> CapitalUpdate:
    """Map debt-maturity/liquidity/capital-allocation shortfalls to a single
    bounded ``survival_multiplier`` (always ``<= 1.0``: shrink-only, never a
    bonus), and future_dilution_capacity to a bounded ``mean_multiplier``.
    Empty feature sets return ``1.0``/``1.0``, reproducing Phase 2-5 output
    exactly -- the regression guard asserted in every phase's test suite.

    ``quality_update`` (Phase 6): future_dilution_capacity must not
    triple-count the same "shares outstanding will grow" story alongside
    v4's ``dilution_drag`` (already inside ``result``, via the growth path)
    and Phase 4's ``per_share_economics`` (already folded into
    ``quality_update.mean_multiplier``). Rather than stacking an independent
    second cap on top, the reduction budget already spent by
    ``quality_update`` is subtracted from the shared
    ``max_combined_dilution_reduction`` ceiling before this signal is
    allowed to spend any of what remains.
    """
    survival_multiplier = 1.0
    mean_multiplier = 1.0
    applied: list[str] = []
    no_effect: list[str] = []
    effects: dict[str, dict] = {}
    already_used_reduction = (
        max(0.0, 1.0 - quality_update.mean_multiplier) if quality_update is not None else 0.0
    )

    for signal in features.signals:
        if not signal.applied or signal.key == excluded_key:
            continue
        value = float(signal.value)
        if signal.key == "future_dilution_capacity":
            overhang = value
            raw_reduction = config.capital.future_dilution_weight * overhang
            remaining_budget = max(
                0.0, config.capital.max_combined_dilution_reduction - already_used_reduction
            )
            reduction = min(
                config.capital.future_dilution_max_reduction, remaining_budget, raw_reduction
            )
            if reduction <= 1e-9:
                # Either the overhang itself rounds to zero, or Phase 4's
                # per_share_economics/incremental_roic already spent the
                # entire shared anti-triple-counting budget -- a genuine
                # no-op, not a fabricated "applied with zero impact" entry
                # (same discipline as the 2026-09-03 incremental_roic fix).
                no_effect.append(signal.key)
                effects[signal.key] = {
                    "status": "no_change_zero_overhang_or_budget_exhausted",
                    "overhang": overhang, "already_used_reduction_budget": already_used_reduction,
                    "remaining_budget": remaining_budget,
                }
                continue
            mean_multiplier *= (1.0 - reduction)
            effects[signal.key] = {
                "overhang": overhang, "raw_reduction": raw_reduction,
                "already_used_reduction_budget": already_used_reduction,
                "remaining_budget": remaining_budget, "reduction": reduction,
                "mean_multiplier_factor": 1.0 - reduction,
            }
            applied.append(signal.key)
            continue
        shortfall = value
        if signal.key == "debt_maturity":
            weight, floor = config.capital.debt_maturity_weight, config.capital.debt_maturity_min_survival_multiplier
        elif signal.key == "liquidity":
            weight = config.capital.liquidity_weight / max(
                config.capital.liquidity_runway_floor_months, 1e-6
            )
            floor = config.capital.liquidity_min_survival_multiplier
        elif signal.key == "capital_allocation":
            weight, floor = config.capital.capital_allocation_weight, config.capital.capital_allocation_min_survival_multiplier
        else:
            continue
        reduction = min(1.0 - floor, weight * shortfall)
        component = 1.0 - reduction
        survival_multiplier *= component
        effects[signal.key] = {
            "shortfall": shortfall, "reduction": reduction, "component": component,
        }
        applied.append(signal.key)

    return CapitalUpdate(
        survival_multiplier=survival_multiplier, mean_multiplier=mean_multiplier,
        applied_keys=tuple(applied), signal_effects=effects, no_effect_keys=tuple(no_effect),
    )
