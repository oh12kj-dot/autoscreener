"""Phase 3 growth, TAM, operating-KPI, consensus, and guidance updates.

All builders are point-in-time, coverage-gated, and return explicit missingness.
Signals update a future state; none is converted into an additive rank score.
"""

from __future__ import annotations

import datetime
import math
import statistics
from dataclasses import asdict, dataclass, replace

from sqlalchemy.orm import Session

from autoscreener.config import ModelV5Config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import (
    AnalystConsensusSnapshot,
    LiveDatasetCoverage,
    ManagementGuidanceSnapshot,
    MarketOpportunityEstimate,
    OperatingKpiDefinition,
    OperatingKpiObservation,
)
from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.inputs import V5PitInput

_FEATURE_KEYS = (
    "tam_headroom", "operating_kpi_nowcast", "consensus_revision", "guidance"
)
_LEDGER_DATASET = {
    "tam_headroom": "market_opportunity",
    "operating_kpi_nowcast": "operating_kpis",
}
_KPI_STOCK_CODES = frozenset({
    "arr", "backlog", "customer_count", "rpo", "gmv", "tpv", "store_count"
})
_KPI_FLOW_CODES = frozenset({"production"})


@dataclass(frozen=True)
class GrowthSignal:
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
class GrowthFeatureSet:
    signals: tuple[GrowthSignal, ...]
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
            # Having an optional observation must not itself create a ranking
            # advantage. Reliability scales its state update; confidence only
            # falls when a coverage-gated source was expected but unavailable.
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

    def excluding(self, key: str) -> GrowthFeatureSet:
        """Return a true leave-one-feature-out set, including confidence effects."""
        return GrowthFeatureSet(
            tuple(
                replace(signal, applied=False, status="ablated")
                if signal.key == key else signal
                for signal in self.signals
            ),
            dict(self.universe_coverage),
        )


@dataclass(frozen=True)
class GrowthUpdate:
    baseline_initial_rate: float
    updated_initial_rate: float
    terminal_rate: float
    baseline_duration_years: float
    updated_duration_years: float
    baseline_fade: float
    updated_fade: float
    revenue_multiple_ratio: float
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]

    def to_dict(self) -> dict:
        return asdict(self)


def _cutoff(as_of: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(
        as_of + datetime.timedelta(days=1), datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )


def _reliability(confidence: str | None) -> float:
    return {
        "manual": 0.95, "high": 0.85, "medium": 0.65,
        "low": 0.30, "unknown": 0.40,
    }.get((confidence or "unknown").lower(), 0.40)


def _coverage_status(rows: list) -> str:
    if not rows:
        return CoverageStatus.NOT_COLLECTED
    return str(rows[-1].coverage_status)


def _empty_signal(key: str, coverage_status: str, status: str | None = None) -> GrowthSignal:
    return GrowthSignal(
        key=key, status=status or coverage_status, coverage_status=coverage_status,
        runtime_enabled=False, applied=False, reliability=0.0,
        observed_at=None, value=None, evidence={},
    )


def _tam_signal(rows: list[MarketOpportunityEstimate]) -> GrowthSignal:
    status = _coverage_status(rows)
    valid_rows = [row for row in rows if str(row.coverage_status) == CoverageStatus.COLLECTED_WITH_DATA]
    if not valid_rows:
        return _empty_signal("tam_headroom", status)
    row = valid_rows[-1]
    reliability = _reliability(row.confidence)
    tam = float(row.tam_value) if row.tam_value is not None else None
    revenue = float(row.current_revenue_addressable) if row.current_revenue_addressable is not None else None
    headroom = tam / revenue if tam and revenue and tam > 0 and revenue > 0 else None
    evidence = {
        "estimate_id": row.id, "tam_value": tam,
        "current_revenue_addressable": revenue, "currency": row.currency,
        "headroom_ratio": headroom, "as_of": row.as_of.isoformat(),
    }
    candidate_status = "candidate"
    if row.currency != "USD" or headroom is None:
        candidate_status = "missing_required_fields"
    return GrowthSignal(
        "tam_headroom", candidate_status, status, False, False, reliability,
        row.observed_at, headroom, evidence,
    )


def _consensus_signal(rows: list[AnalystConsensusSnapshot], as_of: datetime.date) -> GrowthSignal:
    status = _coverage_status(rows)
    groups: dict[tuple[str, str, datetime.date | None], list[AnalystConsensusSnapshot]] = {}
    for row in rows:
        if str(row.coverage_status) != CoverageStatus.COLLECTED_WITH_DATA or row.revenue_mean is None:
            continue
        groups.setdefault((row.source, row.period_type, row.period_end), []).append(row)
    candidates = []
    for key, group in groups.items():
        ordered = sorted(group, key=lambda row: (row.observed_at, row.id))
        distinct = []
        for row in ordered:
            if distinct and row.observed_at == distinct[-1].observed_at:
                distinct[-1] = row
            else:
                distinct.append(row)
        if len(distinct) >= 2:
            candidates.append((distinct[-1].observed_at, key, distinct[-2], distinct[-1]))
    if not candidates:
        return _empty_signal("consensus_revision", status, "insufficient_history")
    _, key, previous, latest = max(candidates, key=lambda item: item[0])
    old = float(previous.revenue_mean)
    new = float(latest.revenue_mean)
    if old <= 0 or new <= 0:
        return _empty_signal("consensus_revision", status, "invalid_nonpositive_value")
    revision = new / old - 1.0
    analyst_factor = min(1.0, (latest.analyst_count or 0) / 5.0)
    reliability = _reliability(latest.confidence) * analyst_factor
    period_end = key[2]
    years = max(1.0, ((period_end - as_of).days / 365.25) if period_end else 1.0)
    return GrowthSignal(
        "consensus_revision", "candidate", status, False, False, reliability,
        latest.observed_at, revision,
        {"previous_id": previous.id, "latest_id": latest.id,
         "previous_revenue_mean": old, "latest_revenue_mean": new,
         "period_end": period_end.isoformat() if period_end else None,
         "years_to_period": years, "analyst_count": latest.analyst_count},
    )


def _kpi_signal(
    rows: list[tuple[OperatingKpiObservation, OperatingKpiDefinition]],
    config: ModelV5Config,
) -> GrowthSignal:
    status = _coverage_status([row for row, _ in rows])
    groups: dict[str, list[tuple[OperatingKpiObservation, OperatingKpiDefinition]]] = {}
    for row, definition in rows:
        if str(row.coverage_status) == CoverageStatus.COLLECTED_WITH_DATA and row.value is not None:
            groups.setdefault(definition.code, []).append((row, definition))
    growths: list[tuple[float, float, dict, datetime.datetime]] = []
    for code, group in groups.items():
        if code not in _KPI_STOCK_CODES | _KPI_FLOW_CODES:
            continue
        by_period: dict[datetime.date, tuple[OperatingKpiObservation, OperatingKpiDefinition]] = {}
        for pair in sorted(group, key=lambda pair: (pair[0].period_end, pair[0].reported_at, pair[0].id)):
            by_period[pair[0].period_end] = pair
        ordered = list(by_period.values())
        if len(ordered) < 2:
            continue
        latest, definition = ordered[-1]
        prior = None
        for candidate, _ in reversed(ordered[:-1]):
            days = (latest.period_end - candidate.period_end).days
            minimum = 300 if code in _KPI_FLOW_CODES else config.growth.min_kpi_comparison_days
            if minimum <= days <= config.growth.max_kpi_comparison_days:
                prior = candidate
                break
        if prior is None:
            continue
        old, new = float(prior.value), float(latest.value)
        days = (latest.period_end - prior.period_end).days
        if old <= 0 or new <= 0:
            continue
        annual_log_growth = math.log(new / old) * 365.25 / days
        if abs(annual_log_growth) > 1.5:
            continue
        annual_growth = math.exp(annual_log_growth) - 1.0
        reliability = min(_reliability(prior.confidence), _reliability(latest.confidence))
        growths.append((annual_growth, reliability, {
            "code": code, "definition_id": definition.id,
            "prior_observation_id": prior.id, "latest_observation_id": latest.id,
            "prior_value": old, "latest_value": new, "comparison_days": days,
            "model_family": definition.model_family,
        }, latest.reported_at))
    if not growths:
        return _empty_signal("operating_kpi_nowcast", status, "insufficient_comparable_history")
    value = statistics.median(item[0] for item in growths)
    reliability = statistics.mean(item[1] for item in growths)
    observed_at = max(item[3] for item in growths)
    return GrowthSignal(
        "operating_kpi_nowcast", "candidate", status, False, False,
        reliability, observed_at, value,
        {"aggregation": "median_comparable_annual_growth",
         "observations": [item[2] for item in growths]},
    )


def _guidance_signal(
    rows: list[ManagementGuidanceSnapshot], item: V5PitInput, as_of: datetime.date
) -> GrowthSignal:
    status = _coverage_status(rows)
    candidates = [
        row for row in rows
        if str(row.coverage_status) == CoverageStatus.COLLECTED_WITH_DATA
        and row.metric.lower() in {"revenue", "sales"}
        and row.period_end is not None and row.period_end > as_of
        and row.unit == "USD" and row.low is not None and row.high is not None
    ]
    if not candidates or item.moic_inputs is None:
        return _empty_signal("guidance", status, "insufficient_valid_revenue_guidance")
    row = candidates[-1]
    low, high = float(row.low), float(row.high)
    trailing = item.moic_inputs.revenue_latest
    if low <= 0 or high < low or trailing <= 0:
        return _empty_signal("guidance", status, "invalid_guidance_range")
    years = max(0.25, (row.period_end - as_of).days / 365.25)
    ratio = ((low + high) / 2.0) / trailing
    if not 0.10 <= ratio <= 10.0:
        return _empty_signal("guidance", status, "unit_or_scale_mismatch")
    implied_growth = ratio ** (1.0 / years) - 1.0
    if not -0.80 <= implied_growth <= 2.0:
        return _empty_signal("guidance", status, "implausible_implied_growth")
    return GrowthSignal(
        "guidance", "candidate", status, False, False,
        _reliability(row.confidence), row.announced_at, implied_growth,
        {"guidance_id": row.id, "low": low, "high": high,
         "period_end": row.period_end.isoformat(), "years_to_period": years,
         "trailing_revenue": trailing, "midpoint_to_trailing_ratio": ratio},
    )


def build_growth_feature_sets(
    session: Session,
    items: list[V5PitInput],
    *,
    as_of: datetime.date,
    config: ModelV5Config,
) -> dict[int, GrowthFeatureSet]:
    """Load Phase 3 datasets in bulk under a strict end-of-day PIT boundary."""
    ticker_ids = [item.ticker_id for item in items]
    if not ticker_ids:
        return {}
    cutoff = _cutoff(as_of)
    consensus_rows = session.query(AnalystConsensusSnapshot).filter(
        AnalystConsensusSnapshot.ticker_id.in_(ticker_ids),
        AnalystConsensusSnapshot.observed_at < cutoff,
    ).order_by(AnalystConsensusSnapshot.observed_at, AnalystConsensusSnapshot.id).all()
    guidance_rows = session.query(ManagementGuidanceSnapshot).filter(
        ManagementGuidanceSnapshot.ticker_id.in_(ticker_ids),
        ManagementGuidanceSnapshot.observed_at < cutoff,
        ManagementGuidanceSnapshot.announced_at < cutoff,
    ).order_by(ManagementGuidanceSnapshot.announced_at, ManagementGuidanceSnapshot.id).all()
    tam_rows = session.query(MarketOpportunityEstimate).filter(
        MarketOpportunityEstimate.ticker_id.in_(ticker_ids),
        MarketOpportunityEstimate.observed_at < cutoff,
        MarketOpportunityEstimate.as_of <= as_of,
    ).order_by(MarketOpportunityEstimate.observed_at, MarketOpportunityEstimate.id).all()
    kpi_rows = session.query(OperatingKpiObservation, OperatingKpiDefinition).join(
        OperatingKpiDefinition,
        OperatingKpiDefinition.id == OperatingKpiObservation.kpi_definition_id,
    ).filter(
        OperatingKpiObservation.ticker_id.in_(ticker_ids),
        OperatingKpiObservation.observed_at < cutoff,
        OperatingKpiObservation.reported_at < cutoff,
        OperatingKpiObservation.period_end <= as_of,
    ).order_by(OperatingKpiObservation.period_end, OperatingKpiObservation.id).all()
    ledger_rows = session.query(LiveDatasetCoverage).filter(
        LiveDatasetCoverage.ticker_id.in_(ticker_ids),
        LiveDatasetCoverage.dataset.in_(list(_LEDGER_DATASET.values())),
        LiveDatasetCoverage.observed_at < cutoff,
    ).order_by(LiveDatasetCoverage.observed_at, LiveDatasetCoverage.id).all()

    def group(rows, ticker_getter=lambda row: row.ticker_id):
        output: dict[int, list] = {ticker_id: [] for ticker_id in ticker_ids}
        for row in rows:
            output[ticker_getter(row)].append(row)
        return output

    consensus_by = group(consensus_rows)
    guidance_by = group(guidance_rows)
    tam_by = group(tam_rows)
    kpi_by = group(kpi_rows, lambda pair: pair[0].ticker_id)
    ledger_by: dict[tuple[int, str], LiveDatasetCoverage] = {}
    for row in ledger_rows:
        ledger_by[(row.ticker_id, row.dataset)] = row

    candidates: dict[int, dict[str, GrowthSignal]] = {}
    item_by_id = {item.ticker_id: item for item in items}
    for ticker_id in ticker_ids:
        candidate = {
            "tam_headroom": _tam_signal(tam_by[ticker_id]),
            "operating_kpi_nowcast": _kpi_signal(kpi_by[ticker_id], config),
            "consensus_revision": _consensus_signal(consensus_by[ticker_id], as_of),
            "guidance": _guidance_signal(guidance_by[ticker_id], item_by_id[ticker_id], as_of),
        }
        for key, dataset in _LEDGER_DATASET.items():
            if candidate[key].coverage_status == CoverageStatus.NOT_COLLECTED:
                ledger = ledger_by.get((ticker_id, dataset))
                if ledger is not None:
                    candidate[key] = replace(
                        candidate[key], coverage_status=str(ledger.coverage_status),
                        status=str(ledger.coverage_status), observed_at=ledger.observed_at,
                        evidence={"coverage_ledger_id": ledger.id,
                                  "reason_code": ledger.reason_code},
                    )
        candidates[ticker_id] = candidate

    coverage = {
        key: sum(
            candidates[ticker_id][key].coverage_status == CoverageStatus.COLLECTED_WITH_DATA
            for ticker_id in ticker_ids
        ) / len(ticker_ids)
        for key in _FEATURE_KEYS
    }
    output: dict[int, GrowthFeatureSet] = {}
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
        output[ticker_id] = GrowthFeatureSet(tuple(signals), dict(coverage))
    return output


def _duration_from_fade(fade: float, horizon_years: int) -> float:
    if fade <= 0:
        return 0.0
    if fade >= 1:
        return float(horizon_years)
    return min(float(horizon_years), math.log(0.5) / math.log(fade))


def _path(initial: float, terminal: float, fade: float, horizon_years: int) -> list[float]:
    return [terminal + (initial - terminal) * fade**year for year in range(1, horizon_years + 1)]


def apply_growth_features(
    result: MoicResult,
    features: GrowthFeatureSet,
    *,
    config: ModelV5Config,
    excluded_key: str | None = None,
) -> GrowthUpdate:
    """Map observations to growth state and a revenue-derived mean multiplier."""
    horizon = config.target_horizon_years
    baseline_initial = result.initial_growth_rate
    terminal = result.terminal_growth_rate
    baseline_fade = result.growth_fade_rate
    baseline_duration = _duration_from_fade(baseline_fade, horizon)
    total_adjustment = 0.0
    duration_cap = float(horizon)
    applied: list[str] = []
    effects: dict[str, dict] = {}
    for signal in features.signals:
        if not signal.applied or signal.key == excluded_key:
            continue
        if signal.key == "consensus_revision":
            years = float(signal.evidence.get("years_to_period") or 1.0)
            annualized = math.log1p(max(-0.95, min(2.0, float(signal.value)))) / years
            adjustment = config.growth.consensus_revision_weight * signal.reliability * annualized
            effects[signal.key] = {"initial_growth_adjustment": adjustment}
            total_adjustment += adjustment
        elif signal.key == "operating_kpi_nowcast":
            gap = float(signal.value) - baseline_initial
            adjustment = config.growth.operating_kpi_weight * signal.reliability * gap
            effects[signal.key] = {"initial_growth_adjustment": adjustment}
            total_adjustment += adjustment
        elif signal.key == "guidance":
            gap = float(signal.value) - baseline_initial
            adjustment = config.growth.guidance_weight * signal.reliability * gap
            effects[signal.key] = {"initial_growth_adjustment": adjustment}
            total_adjustment += adjustment
        elif signal.key == "tam_headroom":
            headroom = float(signal.value)
            if headroom < config.growth.tam_min_headroom_ratio:
                cap = 0.0
            elif baseline_initial > 0.01:
                cap = min(float(horizon), math.log(headroom) / math.log1p(baseline_initial))
            else:
                cap = float(horizon)
            duration_cap = min(duration_cap, cap)
            effects[signal.key] = {"growth_duration_cap_years": cap}
        applied.append(signal.key)

    max_adjustment = config.growth.max_initial_growth_adjustment
    total_adjustment = max(-max_adjustment, min(max_adjustment, total_adjustment))
    updated_initial = max(
        config.growth.min_initial_growth_rate,
        min(config.growth.max_initial_growth_rate, baseline_initial + total_adjustment),
    )
    updated_fade = baseline_fade
    if duration_cap < baseline_duration:
        updated_fade = 0.0 if duration_cap <= 0 else min(
            baseline_fade, 0.5 ** (1.0 / duration_cap)
        )
    updated_duration = _duration_from_fade(updated_fade, horizon)
    baseline_path = _path(baseline_initial, terminal, baseline_fade, horizon)
    updated_path = _path(updated_initial, terminal, updated_fade, horizon)
    baseline_multiple = math.prod(1.0 + rate for rate in baseline_path)
    updated_multiple = math.prod(1.0 + rate for rate in updated_path)
    ratio = updated_multiple / baseline_multiple if baseline_multiple > 0 else 1.0
    return GrowthUpdate(
        baseline_initial_rate=baseline_initial,
        updated_initial_rate=updated_initial,
        terminal_rate=terminal,
        baseline_duration_years=baseline_duration,
        updated_duration_years=updated_duration,
        baseline_fade=baseline_fade,
        updated_fade=updated_fade,
        revenue_multiple_ratio=ratio,
        applied_keys=tuple(applied), signal_effects=effects,
    )
