"""Phase 5 debt-maturity, liquidity, and capital-allocation survival updates.

Mirrors the ``quality.py``/``growth.py`` API by design: every builder is
point-in-time, coverage-gated, and returns explicit missingness. Unlike
Phase 3/4 (which never touched ``survival_probability`` -- "Survival is held
constant until Phase 5/6", scenario.py's old docstring), these three signals
are the first to shrink it. None can ever raise survival above the v4-seeded
baseline; a well-covered maturity wall, ample liquidity, or no aggressive
capital-return commitment all get a no-op multiplier of 1.0, matching the
same never-reward-merely-having-data convention as every earlier phase.

Scope note (see docs/model_v5_phase5_capital_allocation_2026-09-03.md
"Deviations"): a fourth candidate signal, future dilution capacity (ATM/
shelf/unexercised options from ``dilution_capacity``), was in the handoff's
Phase 5 prep list but is deliberately NOT implemented here.
``collect_dilution.py``'s own docstring states that table is never read by
``evaluate_gates`` or ``scoring/`` ("原則3"); wiring it into v5 scoring, even
as an uncertainty-only signal, would silently override that explicit
existing repository principle rather than extend it, so it is left for a
follow-up decision rather than guessed at here.
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
    LiquidityFacility,
    LiveDatasetCoverage,
)
from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.growth import _coverage_status, _cutoff, _reliability
from autoscreener.scoring.v5.inputs import V5PitInput

_FEATURE_KEYS = ("debt_maturity", "liquidity", "capital_allocation")
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
        delta = 0.0
        for signal in self.signals:
            if not signal.runtime_enabled:
                continue
            if signal.coverage_status == CoverageStatus.NOT_COLLECTED:
                delta -= 0.03
            elif signal.coverage_status == CoverageStatus.COLLECTION_FAILED:
                delta -= 0.08
        return max(-0.20, min(0.20, delta))

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
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]

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
        candidate = {
            "debt_maturity": _debt_maturity_signal(
                debt_by[ticker_id], liquidity_row, as_of=as_of
            ),
            "liquidity": _liquidity_signal(liquidity_row, latest_fcf, config=config),
            "capital_allocation": _capital_allocation_signal(
                events_by[ticker_id], liquidity_row, as_of=as_of, config=config
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
            if not configured:
                status = "disabled_by_config"
            elif not runtime_enabled:
                status = "runtime_disabled_low_coverage"
            elif signal.status != "candidate":
                status = signal.status
            elif signal.reliability < spec.min_reliability:
                status = "below_min_reliability"
            elif signal.value is None or abs(signal.value) < 1e-12:
                status = "no_change"
            else:
                status = "applied"
            signals.append(replace(
                signal, status=status, runtime_enabled=runtime_enabled,
                applied=status == "applied",
                evidence={**signal.evidence, "universe_coverage": coverage[key],
                          "required_coverage": spec.required_coverage},
            ))
        output[ticker_id] = CapitalFeatureSet(tuple(signals), dict(coverage))
    return output


def apply_capital_features(
    result: MoicResult,
    features: CapitalFeatureSet,
    *,
    config: ModelV5Config,
    excluded_key: str | None = None,
) -> CapitalUpdate:
    """Map debt-maturity/liquidity/capital-allocation shortfalls to a single
    bounded ``survival_multiplier`` (always ``<= 1.0``: shrink-only, never a
    bonus). Empty feature sets return ``1.0``, reproducing Phase 2-4 output
    exactly -- the regression guard asserted in the Phase 5 tests.
    """
    survival_multiplier = 1.0
    applied: list[str] = []
    effects: dict[str, dict] = {}

    for signal in features.signals:
        if not signal.applied or signal.key == excluded_key:
            continue
        shortfall = float(signal.value)
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
        survival_multiplier=survival_multiplier, applied_keys=tuple(applied),
        signal_effects=effects,
    )
