"""Phase 4 quality, accounting, and reinvestment updates.

Mirrors the ``growth.py`` API by design (docs/model_v5_phase4_handoff_2026-09-03.md
4.1): every builder is point-in-time, coverage-gated, and returns explicit
missingness. Signals here update the future state's uncertainty and a bounded
duration/mean multiplier; none is converted into an additive rank score, and
accounting quality specifically only ever widens sigma and the left tail --
it never lowers a conditional mean (GitHub Issue #3 section 6.3).

Reuses the existing pure calculators (``calculate_reinvestment_quality``,
``calculate_accounting_quality``, ``reconcile``) instead of re-deriving their
formulas, per the handoff instruction not to invent a second definition of
NOPAT/invested-capital/accrual-ratio that could silently drift from the
``/candidates/{ticker}/reinvestment-quality`` and
``/candidates/{ticker}/accounting-quality`` endpoints.
"""

from __future__ import annotations

import datetime
import math
import statistics
from dataclasses import asdict, dataclass, replace

from sqlalchemy.orm import Session

from autoscreener.config import ModelV5Config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import XbrlFact
from autoscreener.scoring.investment_intelligence import ReinvestmentQuality, calculate_reinvestment_quality
from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.growth import GrowthUpdate, _duration_from_fade, _path
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.screening.accounting_quality import AccountingQuality, calculate_accounting_quality
from autoscreener.screening.financial_history import FinancialPeriod
from autoscreener.validation.reconciliation import (
    MAGNITUDE_MISMATCH,
    MISMATCH,
    UNAVAILABLE,
    XbrlFactView,
    reconcile,
)
from autoscreener.validation.xbrl_facts import tag_to_concept

_FEATURE_KEYS = (
    "incremental_roic", "per_share_economics", "cash_conversion",
    "accounting_quality", "reconciliation_confidence",
)

# Financial-statement-derived ratios are not LLM extractions or third-party
# estimates; they come directly from the same reported figures v4 already
# scores on. Reconciliation is a deterministic match/mismatch classification.
_STATEMENT_RELIABILITY = 0.90
_RECONCILIATION_RELIABILITY = 1.0


@dataclass(frozen=True)
class QualitySignal:
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
class QualityFeatureSet:
    signals: tuple[QualitySignal, ...]
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
            # Same missingness contract as growth.py: an optional observation
            # never grants a confidence bonus, only its absence (when the
            # source is otherwise expected) lowers confidence.
            if signal.coverage_status == CoverageStatus.NOT_COLLECTED:
                delta -= 0.03
            elif signal.coverage_status == CoverageStatus.COLLECTION_FAILED:
                delta -= 0.08
            if signal.key == "reconciliation_confidence" and signal.applied:
                delta -= float(signal.evidence.get("confidence_penalty") or 0.0)
        return max(-0.20, min(0.20, delta))

    def to_dict(self) -> dict:
        return {
            "universe_coverage": self.universe_coverage,
            "signals": [signal.to_dict() for signal in self.signals],
            "applied_keys": list(self.applied_keys),
            "confidence_delta": self.confidence_delta,
        }

    def excluding(self, key: str) -> QualityFeatureSet:
        """Return a true leave-one-feature-out set, including confidence effects."""
        return QualityFeatureSet(
            tuple(
                replace(signal, applied=False, status="ablated")
                if signal.key == key else signal
                for signal in self.signals
            ),
            dict(self.universe_coverage),
        )


@dataclass(frozen=True)
class QualityUpdate:
    duration_multiplier: float
    mean_multiplier: float
    sigma_multiplier: float
    left_tail_extra: float
    confidence_penalty: float
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]
    # Signals whose feature-level candidacy status was "applied" (nonzero
    # raw value) but whose actual effect on this ticker's state/distribution
    # resolved to exactly zero this run (e.g. incremental_roic's duration
    # shortfall multiplied by a non-positive growth level). Recorded so the
    # engine can downgrade the persisted signal to "no_change" instead of an
    # honest-looking but functionally inert "applied" (audit fix, 2026-09-03:
    # 214/214 incremental_roic ablations previously showed zero distribution
    # impact because duration never fed back into the growth path).
    no_effect_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_signal(key: str, coverage_status: str, status: str | None = None) -> QualitySignal:
    return QualitySignal(
        key=key, status=status or coverage_status, coverage_status=coverage_status,
        runtime_enabled=False, applied=False, reliability=0.0,
        observed_at=None, value=None, evidence={},
    )


def _period_datetime(period_end: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(period_end, datetime.time.min, tzinfo=datetime.timezone.utc)


def _nopat(operating_income: float | None, tax_rate: float) -> float | None:
    """Same NOPAT proxy as routes.py:3539-3540 (0.79 there == 1 - 0.21 here), config-driven."""
    if operating_income is None or operating_income <= 0:
        return None
    return operating_income * (1.0 - tax_rate)


def _invested_capital(total_debt: float | None, cash: float | None) -> float | None:
    """Same proxy as routes.py:3541-3542: total_debt - cash_and_equivalents."""
    if total_debt is None or cash is None:
        return None
    return total_debt - cash


def _measurement_years(
    start: FinancialPeriod, end: FinancialPeriod, config: ModelV5Config
) -> tuple[float | None, str | None]:
    days = (end.period_end - start.period_end).days
    if days < config.quality.min_measurement_days:
        return None, "measurement_window_too_short"
    years = days / 365.25
    if years > config.quality.max_measurement_years:
        return None, "measurement_window_too_long"
    return years, None


def _annual_window(
    annual: tuple[FinancialPeriod, ...], min_periods: int
) -> tuple[str, str | None]:
    if not annual:
        return CoverageStatus.NOT_COLLECTED, "no_raw_snapshot"
    if len(annual) < min_periods:
        return CoverageStatus.NOT_COLLECTED, "insufficient_annual_history"
    return CoverageStatus.COLLECTED_WITH_DATA, None


def _reinvestment_quality(
    start: FinancialPeriod, end: FinancialPeriod, years: float, config: ModelV5Config
) -> ReinvestmentQuality:
    nopat_start = _nopat(start.operating_income, config.quality.nopat_tax_rate)
    nopat_end = _nopat(end.operating_income, config.quality.nopat_tax_rate)
    ic_start = _invested_capital(start.total_debt, start.cash_and_equivalents)
    ic_end = _invested_capital(end.total_debt, end.cash_and_equivalents)
    return calculate_reinvestment_quality(
        years=years, revenue_start=start.revenue, revenue_end=end.revenue,
        gross_profit_start=start.gross_profit, gross_profit_end=end.gross_profit,
        fcf_start=start.free_cash_flow, fcf_end=end.free_cash_flow,
        shares_start=start.shares_outstanding, shares_end=end.shares_outstanding,
        nopat_start=nopat_start, nopat_end=nopat_end,
        invested_capital_start=ic_start, invested_capital_end=ic_end,
    )


def _incremental_roic_signal(
    quality: ReinvestmentQuality | None,
    annual: tuple[FinancialPeriod, ...],
    years: float | None,
    reason: str | None,
    config: ModelV5Config,
) -> QualitySignal:
    status, base_reason = _annual_window(annual, config.quality.min_annual_periods)
    if status != CoverageStatus.COLLECTED_WITH_DATA:
        return _empty_signal("incremental_roic", status, base_reason)
    if quality is None or years is None:
        return _empty_signal("incremental_roic", CoverageStatus.COLLECTED_WITH_DATA, reason)
    if quality.incremental_roic is None:
        # DeltaIC <= 0, or NOPAT missing/non-positive in either period: not a
        # penalty, a genuine "cannot be computed" (handoff 4.4 corner cases).
        return _empty_signal(
            "incremental_roic", CoverageStatus.COLLECTED_WITH_DATA,
            "delta_ic_non_positive_or_nopat_unavailable",
        )
    end = annual[-1]
    shortfall = max(0.0, config.quality.incremental_roic_hurdle_rate - quality.incremental_roic)
    evidence = {
        "incremental_roic": quality.incremental_roic,
        "reinvestment_rate": quality.reinvestment_rate,
        "hurdle_rate": config.quality.incremental_roic_hurdle_rate,
        "shortfall_below_hurdle": shortfall,
        "years": years,
        "period_end": end.period_end.isoformat(),
    }
    return QualitySignal(
        "incremental_roic", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _STATEMENT_RELIABILITY, _period_datetime(end.period_end), shortfall, evidence,
    )


def _per_share_economics_signal(
    quality: ReinvestmentQuality | None,
    annual: tuple[FinancialPeriod, ...],
    years: float | None,
    reason: str | None,
    config: ModelV5Config,
) -> QualitySignal:
    status, base_reason = _annual_window(annual, config.quality.min_annual_periods)
    if status != CoverageStatus.COLLECTED_WITH_DATA:
        return _empty_signal("per_share_economics", status, base_reason)
    if quality is None or years is None:
        return _empty_signal("per_share_economics", CoverageStatus.COLLECTED_WITH_DATA, reason)
    # Deliberately excludes revenue: the revenue-per-share gap is driven by
    # the same shares_outstanding denominator that v4's dilution_drag
    # (capital.diluted_share_factor, seeded from MoicInputs.dilution_cagr)
    # already prices in. Including it here would double-count the identical
    # share-count effect through two separate mean multipliers. Gross profit
    # and FCF per-share gaps still carry incremental information (margin and
    # cash-generation dilution beyond pure share count).
    gaps = []
    if quality.gross_profit_cagr is not None and quality.gross_profit_per_share_cagr is not None:
        gaps.append(quality.gross_profit_cagr - quality.gross_profit_per_share_cagr)
    if quality.fcf_cagr is not None and quality.fcf_per_share_cagr is not None:
        gaps.append(quality.fcf_cagr - quality.fcf_per_share_cagr)
    if not gaps:
        return _empty_signal(
            "per_share_economics", CoverageStatus.COLLECTED_WITH_DATA,
            "shares_outstanding_or_metric_unavailable",
        )
    end = annual[-1]
    gap = max(0.0, statistics.mean(gaps))
    evidence = {
        "gross_profit_cagr": quality.gross_profit_cagr,
        "gross_profit_per_share_cagr": quality.gross_profit_per_share_cagr,
        "fcf_cagr": quality.fcf_cagr,
        "fcf_per_share_cagr": quality.fcf_per_share_cagr,
        "excludes_revenue_to_avoid_double_count_with_dilution_drag": True,
        "dilutive_gap": gap, "years": years, "period_end": end.period_end.isoformat(),
    }
    return QualitySignal(
        "per_share_economics", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _STATEMENT_RELIABILITY, _period_datetime(end.period_end), gap, evidence,
    )


def _cash_conversion_signal(
    annual: tuple[FinancialPeriod, ...], config: ModelV5Config
) -> QualitySignal:
    if not annual:
        return _empty_signal("cash_conversion", CoverageStatus.NOT_COLLECTED, "no_raw_snapshot")
    latest = annual[-1]
    ni, ocf, fcf, revenue = (
        latest.net_income, latest.operating_cash_flow, latest.free_cash_flow, latest.revenue,
    )
    if ni is None or ocf is None:
        return _empty_signal(
            "cash_conversion", CoverageStatus.COLLECTED_WITH_DATA, "net_income_or_ocf_unavailable"
        )
    floor = (
        config.quality.cash_conversion_ni_floor_ratio * abs(revenue)
        if revenue is not None else 0.0
    )
    if abs(ni) < max(floor, 1e-6):
        return _empty_signal(
            "cash_conversion", CoverageStatus.COLLECTED_WITH_DATA, "net_income_near_zero"
        )
    bound = config.quality.cash_conversion_ratio_winsor_abs
    ocf_ratio = max(-bound, min(bound, ocf / ni))
    fcf_ratio = max(-bound, min(bound, fcf / ni)) if fcf is not None else None
    evidence = {
        "cash_conversion_ocf_ni": ocf_ratio, "cash_conversion_fcf_ni": fcf_ratio,
        "net_income": ni, "operating_cash_flow": ocf, "free_cash_flow": fcf,
        "period_end": latest.period_end.isoformat(),
    }
    return QualitySignal(
        "cash_conversion", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _STATEMENT_RELIABILITY, _period_datetime(latest.period_end), ocf_ratio, evidence,
    )


_ACCOUNTING_CHECKS: tuple[tuple[str, str], ...] = (
    ("accrual_ratio", "high_accruals"),
    ("cash_conversion", "weak_cash_conversion"),
    ("receivables_gap", "receivables_outpacing_revenue"),
    ("inventory_gap", "inventory_outpacing_revenue"),
    ("sbc_to_revenue", "high_sbc_to_revenue"),
    ("goodwill_to_assets", "high_goodwill_to_assets"),
)


def _accounting_severity(quality: AccountingQuality) -> float | None:
    """Fraction of computable checks that actually triggered a warning.

    Only counts a check as computable when its underlying ratio was not
    None; a check whose input rows this repository does not have never
    contributes a fabricated zero (handoff 4.2 known FinancialPeriod gap).
    """
    computable = [
        warning for field, warning in _ACCOUNTING_CHECKS
        if getattr(quality, field) is not None
    ]
    if not computable:
        return None
    triggered = sum(1 for warning in computable if warning in quality.warnings)
    return triggered / len(computable)


def _yoy(prior: float | None, latest: float | None) -> float | None:
    if prior is None or latest is None or prior == 0:
        return None
    return latest / prior - 1.0


def _average(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return (a + b) / 2.0


def _accounting_quality_signal(
    annual: tuple[FinancialPeriod, ...], config: ModelV5Config
) -> QualitySignal:
    status, base_reason = _annual_window(annual, config.quality.min_annual_periods)
    if status != CoverageStatus.COLLECTED_WITH_DATA:
        return _empty_signal("accounting_quality", status, base_reason)
    latest, prior = annual[-1], annual[-2]
    revenue_growth = _yoy(prior.revenue, latest.revenue)
    quality = calculate_accounting_quality(
        net_income=latest.net_income, operating_cash_flow=latest.operating_cash_flow,
        average_assets=_average(prior.total_assets, latest.total_assets),
        revenue_growth=revenue_growth,
        receivables_growth=_yoy(prior.accounts_receivable, latest.accounts_receivable),
        inventory_growth=_yoy(prior.inventory, latest.inventory),
        stock_based_compensation=latest.stock_based_compensation, revenue=latest.revenue,
        goodwill=latest.goodwill, total_assets=latest.total_assets,
    )
    severity = _accounting_severity(quality)
    if severity is None:
        return _empty_signal(
            "accounting_quality", CoverageStatus.COLLECTED_WITH_DATA,
            "accounting_quality_inputs_unavailable",
        )
    evidence = {
        "accrual_ratio": quality.accrual_ratio, "cash_conversion": quality.cash_conversion,
        "receivables_gap": quality.receivables_gap, "inventory_gap": quality.inventory_gap,
        "sbc_to_revenue": quality.sbc_to_revenue, "goodwill_to_assets": quality.goodwill_to_assets,
        "warnings": list(quality.warnings), "severity": severity,
        "period_end": latest.period_end.isoformat(),
    }
    return QualitySignal(
        "accounting_quality", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _STATEMENT_RELIABILITY, _period_datetime(latest.period_end), severity, evidence,
    )


def _reconciliation_signal(
    item: V5PitInput, xbrl_rows: list[XbrlFact], *, as_of: datetime.date
) -> QualitySignal:
    if not item.financial_annual:
        return _empty_signal(
            "reconciliation_confidence", CoverageStatus.NOT_COLLECTED, "no_raw_snapshot"
        )
    latest = item.financial_annual[-1]
    model_inputs = {
        "revenue": latest.revenue,
        "shares_outstanding": latest.shares_outstanding,
        "cash": latest.cash_and_equivalents,
        # Total Liabilities Net Minority Interest is not a FinancialPeriod
        # field (handoff 4.2/§7.5: never fill an absent field with a
        # different concept such as total_debt). Left unavailable rather
        # than estimated.
        "liabilities": None,
    }
    if all(value is None for value in model_inputs.values()):
        return _empty_signal(
            "reconciliation_confidence", CoverageStatus.COLLECTED_WITH_DATA,
            "model_inputs_unavailable",
        )
    facts = [
        XbrlFactView(
            concept=tag_to_concept(row.taxonomy, row.tag) or "", tag=row.tag,
            value=float(row.value), period_end=row.period_end, filed_date=row.filed_date,
            period_start=row.period_start,
        )
        for row in xbrl_rows
        if row.filed_date <= as_of and tag_to_concept(row.taxonomy, row.tag) is not None
    ]
    if not facts:
        return _empty_signal(
            "reconciliation_confidence", CoverageStatus.NOT_COLLECTED, "no_xbrl_facts_as_of"
        )
    items = reconcile(model_inputs, facts, as_of=as_of)
    comparable = [row for row in items if row.status != UNAVAILABLE]
    if not comparable:
        return _empty_signal(
            "reconciliation_confidence", CoverageStatus.COLLECTED_WITH_DATA,
            "no_comparable_concepts",
        )
    mismatches = [row for row in comparable if row.status in (MISMATCH, MAGNITUDE_MISMATCH)]
    magnitude = any(row.status == MAGNITUDE_MISMATCH for row in mismatches)
    severity = len(mismatches) / len(comparable)
    evidence = {
        "comparable_concepts": len(comparable), "mismatch_count": len(mismatches),
        "magnitude_mismatch": magnitude, "severity": severity,
        "items": [
            {
                "concept": row.concept, "status": row.status,
                # reconciliation.py's zero-denominator branch can produce
                # float("inf"); JSONB (and json.dumps' strict mode) rejects
                # Infinity/NaN, so it is recorded as a bounded sentinel
                # instead of dropping the row or crashing the persist step.
                "relative_diff": (
                    row.relative_diff if row.relative_diff is None or math.isfinite(row.relative_diff)
                    else None
                ),
                "relative_diff_is_unbounded": (
                    row.relative_diff is not None and not math.isfinite(row.relative_diff)
                ),
                "sec_period_end": row.sec_period_end.isoformat() if row.sec_period_end else None,
                "sec_filed_date": row.sec_filed_date.isoformat() if row.sec_filed_date else None,
            }
            for row in comparable
        ],
    }
    observed_at = max(
        (row.sec_filed_date for row in comparable if row.sec_filed_date is not None),
        default=latest.period_end,
    )
    return QualitySignal(
        "reconciliation_confidence", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        _RECONCILIATION_RELIABILITY, _period_datetime(observed_at), severity, evidence,
    )


def build_quality_feature_sets(
    session: Session,
    items: list[V5PitInput],
    *,
    as_of: datetime.date,
    config: ModelV5Config,
) -> dict[int, QualityFeatureSet]:
    """Load Phase 4 signals in bulk under the same end-of-day PIT boundary."""
    ticker_ids = [item.ticker_id for item in items]
    if not ticker_ids:
        return {}
    xbrl_rows = session.query(XbrlFact).filter(
        XbrlFact.ticker_id.in_(ticker_ids), XbrlFact.filed_date <= as_of,
    ).all()
    xbrl_by_ticker: dict[int, list[XbrlFact]] = {ticker_id: [] for ticker_id in ticker_ids}
    for row in xbrl_rows:
        xbrl_by_ticker[row.ticker_id].append(row)

    candidates: dict[int, dict[str, QualitySignal]] = {}
    for item in items:
        annual = item.financial_annual
        quality: ReinvestmentQuality | None = None
        years: float | None = None
        reason: str | None = None
        if len(annual) >= config.quality.min_annual_periods:
            start, end = annual[0], annual[-1]
            years, reason = _measurement_years(start, end, config)
            if years is not None:
                quality = _reinvestment_quality(start, end, years, config)
        candidates[item.ticker_id] = {
            "incremental_roic": _incremental_roic_signal(quality, annual, years, reason, config),
            "per_share_economics": _per_share_economics_signal(quality, annual, years, reason, config),
            "cash_conversion": _cash_conversion_signal(annual, config),
            "accounting_quality": _accounting_quality_signal(annual, config),
            "reconciliation_confidence": _reconciliation_signal(
                item, xbrl_by_ticker[item.ticker_id], as_of=as_of
            ),
        }

    coverage = {
        key: sum(
            candidates[ticker_id][key].coverage_status == CoverageStatus.COLLECTED_WITH_DATA
            for ticker_id in ticker_ids
        ) / len(ticker_ids)
        for key in _FEATURE_KEYS
    }
    output: dict[int, QualityFeatureSet] = {}
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
            evidence = {**signal.evidence, "universe_coverage": coverage[key],
                        "required_coverage": spec.required_coverage}
            if key == "reconciliation_confidence" and status == "applied":
                # Bounded penalty computed here (config-driven) so
                # confidence_delta above can read it back without
                # recomputing the reconcile() classification.
                config_penalty = config.quality.reconciliation_confidence_penalty
                scale = 2.0 if evidence.get("magnitude_mismatch") else 1.0
                evidence["confidence_penalty"] = min(
                    config_penalty, config_penalty * float(signal.value) * scale
                )
            signals.append(replace(
                signal, status=status, runtime_enabled=runtime_enabled,
                applied=status == "applied", evidence=evidence,
            ))
        output[ticker_id] = QualityFeatureSet(tuple(signals), dict(coverage))
    return output


def apply_quality_features(
    result: MoicResult,
    features: QualityFeatureSet,
    *,
    config: ModelV5Config,
    growth_update: GrowthUpdate | None = None,
    excluded_key: str | None = None,
) -> QualityUpdate:
    """Map quality observations to bounded state/uncertainty multipliers.

    Every multiplier defaults to a no-op (1.0 / 0.0) so an empty feature set
    reproduces Phase 2/3 output exactly -- this is the regression guard
    referenced in the handoff (4.5) and asserted in the Phase 4 tests.

    ``growth_update`` (audit fix, 2026-09-03) is the already-applied Phase 3
    growth state: incremental_roic's duration shortening must compose with
    the growth-adjusted path (initial rate, terminal rate, fade), not just
    the raw v4 seed, or it only ever changes a diagnostic display number and
    never the distribution -- the original defect (214/214 real ablations
    showed zero P(target)/expected_cagr impact because duration_multiplier
    fed only the display state and the ablation's own state_shift, never
    build_scenarios). When omitted (direct unit-test calls with no growth
    context) this falls back to the raw seed.
    """
    duration_multiplier = 1.0
    mean_multiplier = 1.0
    sigma_multiplier = 1.0
    left_tail_extra = 0.0
    confidence_penalty = 0.0
    applied: list[str] = []
    no_effect: list[str] = []
    effects: dict[str, dict] = {}

    horizon = config.target_horizon_years
    if growth_update is not None:
        current_initial = growth_update.updated_initial_rate
        current_terminal = growth_update.terminal_rate
        current_fade = growth_update.updated_fade
        current_duration = growth_update.updated_duration_years
    else:
        current_initial = result.initial_growth_rate
        current_terminal = result.terminal_growth_rate
        current_fade = result.growth_fade_rate
        current_duration = _duration_from_fade(current_fade, horizon)
    growth_level = max(0.0, current_initial)

    for signal in features.signals:
        if not signal.applied or signal.key == excluded_key:
            continue
        if signal.key == "incremental_roic":
            shortfall = float(signal.value)
            raw_years = (
                config.quality.incremental_roic_weight * growth_level * shortfall * horizon
            )
            reduction_years = min(config.quality.max_duration_reduction_years, raw_years)
            if reduction_years <= 1e-9:
                # Growth is not elevated (or the clamped shortfall resolves
                # to zero reduction): incremental ROIC has no observable
                # effect for this ticker this run. Recorded honestly as a
                # no-op instead of counted as "applied" with a fabricated
                # zero impact (audit fix 1, 2026-09-03).
                no_effect.append(signal.key)
                effects[signal.key] = {
                    "status": "no_change_zero_growth_or_reduction",
                    "current_duration_years": current_duration,
                    "growth_level": growth_level, "shortfall": shortfall,
                }
                continue
            new_duration = max(0.0, current_duration - reduction_years)
            duration_multiplier = (
                1.0 if current_duration <= 0 else new_duration / current_duration
            )
            new_fade = (
                0.0 if new_duration <= 0
                else min(current_fade, 0.5 ** (1.0 / new_duration))
            )
            baseline_path = _path(current_initial, current_terminal, current_fade, horizon)
            reduced_path = _path(current_initial, current_terminal, new_fade, horizon)
            baseline_multiple = math.prod(1.0 + rate for rate in baseline_path)
            reduced_multiple = math.prod(1.0 + rate for rate in reduced_path)
            incremental_ratio = (
                reduced_multiple / baseline_multiple if baseline_multiple > 0 else 1.0
            )
            # Composes multiplicatively with per_share_economics below and
            # with growth_update.revenue_multiple_ratio in engine.py, so the
            # duration compression this signal adds on top of Phase 3's
            # already-applied growth path is counted exactly once.
            mean_multiplier *= incremental_ratio
            effects[signal.key] = {
                "current_duration_years": current_duration,
                "reduction_years": reduction_years,
                "duration_multiplier": duration_multiplier,
                "revenue_multiple_ratio_from_duration": incremental_ratio,
            }
        elif signal.key == "per_share_economics":
            gap = float(signal.value)
            raw_reduction = config.quality.per_share_gap_weight * gap
            reduction = min(config.quality.max_mean_multiplier_reduction, raw_reduction)
            mean_multiplier *= (1.0 - reduction)
            effects[signal.key] = {
                "dilutive_gap": gap, "mean_multiplier_reduction": reduction,
                "mean_multiplier_factor": 1.0 - reduction,
            }
        elif signal.key == "cash_conversion":
            # Diagnostic-only (handoff 4.3): populates
            # economics.cash_conversion / economics.reinvestment_efficiency
            # state values but never a distribution multiplier, so this
            # "applied" entry's ablation is identically zero on
            # p_target/expected_cagr by design, not a bug (see Phase 4 doc).
            evidence = signal.evidence
            effects[signal.key] = {
                "cash_conversion": evidence.get("cash_conversion_ocf_ni"),
                "reinvestment_efficiency": evidence.get("cash_conversion_fcf_ni"),
            }
        elif signal.key == "accounting_quality":
            severity = min(1.0, float(signal.value))
            sigma_multiplier = 1.0 + (
                config.quality.accounting_sigma_max_multiplier - 1.0
            ) * severity
            left_tail_extra = config.quality.accounting_left_tail_extra_max * severity
            effects[signal.key] = {
                "severity": severity, "sigma_multiplier": sigma_multiplier,
                "left_tail_extra": left_tail_extra,
            }
        elif signal.key == "reconciliation_confidence":
            confidence_penalty = float(signal.evidence.get("confidence_penalty") or 0.0)
            effects[signal.key] = {
                "severity": float(signal.value), "confidence_penalty": confidence_penalty,
            }
        applied.append(signal.key)

    return QualityUpdate(
        duration_multiplier=duration_multiplier, mean_multiplier=mean_multiplier,
        sigma_multiplier=sigma_multiplier, left_tail_extra=left_tail_extra,
        confidence_penalty=confidence_penalty, applied_keys=tuple(applied),
        signal_effects=effects, no_effect_keys=tuple(no_effect),
    )
