"""D-1 (docs/racr_wp_d_reliability_layer_2026-09-04.md): a real reliability
function, replacing the flat ``model_confidence = 0.5`` constant that made
RACR's uncertainty term degenerate into ``0.25*|ce_cagr|`` -- see
``docs/racr_shadow_run_diagnostic_2026-09-04.md`` §3.2 and
``docs/racr_wp_b2_risk_terms_2026-09-04.md`` §4.

Implements audit §7.3 exactly::

    r_x = q_source * q_extract * q_PIT * q_sample * q_reconcile * freshness(age)
    freshness(age) = exp(-ln2 * age / halfLife_x)

Two things this module deliberately does NOT do (see
``docs/racr_wp_d_reliability_layer_2026-09-04.md`` for the full reasoning
and measured numbers):

- ``q_reconcile`` is held at a named, inert constant (``Q_RECONCILE_INERT =
  1.0``) for every ticker. Cross-source reconciliation against XBRL is
  WP-D's stated scope in the plan, but ``xbrl_facts`` covers only
  291/5,893 tickers (2026-09-04) -- nowhere near universe-wide -- so there
  is no honest way to fold a real reconciliation-quality signal into a
  factor that must exist for every ticker. Inventing a number for the
  other ~95% would just reproduce the defect this module exists to fix (a
  fabricated universal constant). ``quality.py``'s own
  ``reconciliation_confidence`` signal already penalizes
  ``model_confidence`` directly on the subset of tickers where a real
  yfinance-vs-XBRL comparison exists; ``q_reconcile`` here is a distinct,
  additional factor that stays inert until a broader signal exists.

- ``available_from`` is never used as a freshness signal (WP-D trap 1): it
  is the ingestion date, not the age of the underlying evidence. Measured
  across the scored universe: age min 0, median 0, p90 0, max 1 day --
  universe-wide near-constant, exactly the failure mode this module exists
  to avoid. The real reporting lag comes from the statement period-end
  dates already carried on ``V5PitInput.financial_annual``.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from autoscreener.coverage import CoverageStatus

if TYPE_CHECKING:
    from autoscreener.config import ModelV5Config, ModelV5ReliabilityConfig
    from autoscreener.scoring.v5.inputs import V5PitInput

# See module docstring: deliberately inert until a universe-wide cross-source
# reconciliation-quality signal exists. Never silently folded into another
# factor -- always surfaced under this exact name wherever r_x is reported.
Q_RECONCILE_INERT = 1.0

# Canonical annual-statement fields used to score q_extract (payload-field
# completeness). Deliberately a fixed, small, "reported by nearly every
# operating company regardless of sector" set, rather than every
# FinancialPeriod field -- optional/rarer fields (goodwill, inventory,
# receivables, SBC) are not universally reported even for healthy companies
# (e.g. asset-light businesses), so requiring them here would score sector
# composition, not evidence quality.
REQUIRED_STATEMENT_FIELDS: tuple[str, ...] = (
    "revenue", "gross_profit", "operating_income", "net_income",
    "operating_cash_flow", "free_cash_flow", "cash_and_equivalents",
    "total_debt", "shares_outstanding", "total_assets",
)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def freshness(age_days: float | None, half_life_days: float | None) -> float:
    """``exp(-ln2 * age / halfLife)`` -- audit §7.3.

    Returns ``1.0`` (no decay) when age or half-life is unknown or
    non-positive: an explicit neutral default, not a fabricated penalty,
    matching the "no penalty for what cannot be measured" convention used
    throughout ``scoring/v5``.
    """
    if age_days is None or half_life_days is None or half_life_days <= 0:
        return 1.0
    age = max(0.0, float(age_days))
    return math.exp(-math.log(2.0) * age / float(half_life_days))


def reliability_weight(
    *,
    q_source: float,
    q_extract: float,
    q_pit: float,
    q_sample: float,
    age_days: float | None = None,
    half_life_days: float | None = None,
    q_reconcile: float = Q_RECONCILE_INERT,
) -> float:
    """audit §7.3's ``r_x``. Every ``q_*`` is clamped to ``[0, 1]``
    defensively -- a caller bug upstream must not silently produce a
    reliability outside its contractual range."""
    return (
        _clamp01(q_source) * _clamp01(q_extract) * _clamp01(q_pit) * _clamp01(q_sample)
        * _clamp01(q_reconcile) * freshness(age_days, half_life_days)
    )


@dataclass(frozen=True)
class CoreEvidenceReliability:
    """The audit §7.3 factors for one ticker's *always-present* evidence
    (financial statements + price history) -- as opposed to the optional
    Live Intelligence signals in growth.py/quality.py/balance_sheet.py/
    tail_risk.py, each of which already carries its own per-row
    reliability but is only present for 0.24%-25% of the universe.

    This is what makes ``model_confidence`` vary universe-wide: every
    component here is measurable (even if at a degraded value) for any
    ticker that has a distribution at all.
    """

    q_source: float
    q_extract: float
    q_pit: float
    q_sample: float
    reporting_lag_days: float | None
    half_life_days: float
    annual_periods: int
    price_row_count: int
    value: float

    def to_dict(self) -> dict:
        return {
            "q_source": self.q_source,
            "q_extract": self.q_extract,
            "q_pit": self.q_pit,
            "q_sample": self.q_sample,
            "q_reconcile": Q_RECONCILE_INERT,
            "reporting_lag_days": self.reporting_lag_days,
            "half_life_days": self.half_life_days,
            "annual_periods": self.annual_periods,
            "price_row_count": self.price_row_count,
            "value": self.value,
        }


def _extract_completeness(item: "V5PitInput") -> float:
    if not item.financial_annual:
        return 0.0
    latest = item.financial_annual[-1]
    present = sum(
        1 for field in REQUIRED_STATEMENT_FIELDS
        if getattr(latest, field, None) is not None
    )
    return present / len(REQUIRED_STATEMENT_FIELDS)


def _sample_adequacy(item: "V5PitInput", config: "ModelV5ReliabilityConfig") -> float:
    periods_ratio = min(1.0, len(item.financial_annual) / config.target_annual_periods)
    price_ratio = min(1.0, item.price_row_count / config.target_price_history_rows)
    return (periods_ratio + price_ratio) / 2.0


def _source_quality(item: "V5PitInput") -> float:
    """``RawSnapshot.is_valid``/``validation_errors`` are already-stored,
    per-ticker source-quality evidence (``validation/rules.py``'s range and
    cross-field checks) -- not a fabricated per-ticker number."""
    if item.raw_is_valid and item.raw_validation_error_count == 0:
        return 1.0
    penalty = 0.0 if item.raw_is_valid else 0.15
    penalty += min(0.45, 0.10 * item.raw_validation_error_count)
    return _clamp01(1.0 - penalty)


def _pit_quality(item: "V5PitInput", *, as_of: datetime.date) -> float:
    """A modest, bounded discount when the price series itself is stale as
    of the scoring date -- evidence the PIT snapshot may not reflect
    current trading (feed outage, halted/illiquid name). Distinct from the
    reporting-lag freshness decay below, which is about financial-statement
    age, not price recency."""
    if item.price_as_of is None:
        return 0.85
    stale_days = (as_of - item.price_as_of).days
    if stale_days <= 5:
        return 1.0
    return _clamp01(1.0 - min(0.5, (stale_days - 5) / 400.0))


def core_evidence_reliability(
    item: "V5PitInput", *, as_of: datetime.date, config: "ModelV5ReliabilityConfig",
) -> CoreEvidenceReliability:
    """audit §7.3's ``r_x`` for the always-present base evidence.

    WP-D trap 1 (see module docstring): age is measured from the latest
    annual statement's ``period_end`` (already PIT-filtered to
    ``period_end <= as_of`` by ``inputs.py``), never from
    ``available_from``.
    """
    q_source = _source_quality(item)
    q_extract = _extract_completeness(item)
    q_pit = _pit_quality(item, as_of=as_of)
    q_sample = _sample_adequacy(item, config)
    reporting_lag_days: float | None = None
    if item.financial_annual:
        reporting_lag_days = float((as_of - item.financial_annual[-1].period_end).days)
    value = reliability_weight(
        q_source=q_source, q_extract=q_extract, q_pit=q_pit, q_sample=q_sample,
        age_days=reporting_lag_days, half_life_days=config.statement_freshness_half_life_days,
    )
    return CoreEvidenceReliability(
        q_source=q_source, q_extract=q_extract, q_pit=q_pit, q_sample=q_sample,
        reporting_lag_days=reporting_lag_days,
        half_life_days=config.statement_freshness_half_life_days,
        annual_periods=len(item.financial_annual), price_row_count=item.price_row_count,
        value=value,
    )


def base_confidence_for(
    item: "V5PitInput", *, as_of: datetime.date, config: "ModelV5Config", has_distribution: bool,
) -> tuple[float, CoreEvidenceReliability | None]:
    """Replaces the old ``model_config.reliability.ready_input_confidence``
    flat constant (0.5 for every ticker -- the root cause diagnosed in
    docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2). Returns
    ``(base_confidence, core_evidence)``; ``core_evidence`` is ``None``
    exactly when there is no distribution to score at all (mirrors the old
    ``unavailable_input_confidence`` branch -- no evidence quality can be
    measured for a ticker with no ``MoicResult``).
    """
    reliability_config = config.reliability
    if not has_distribution:
        return reliability_config.unavailable_input_confidence, None
    evidence = core_evidence_reliability(item, as_of=as_of, config=reliability_config)
    span = reliability_config.max_base_confidence - reliability_config.min_base_confidence
    confidence = reliability_config.min_base_confidence + span * evidence.value
    return _clamp01(confidence), evidence


def decayed_reliability(signal, *, half_life_days: float | None, as_of: datetime.date) -> float:
    """D-3: wires ``FeatureSpec.freshness_half_life_days`` (previously dead
    metadata -- zero runtime references outside ``feature_registry.py``)
    into the reliability actually used to gate/scale a signal's state
    update. Duck-typed over any of the four ``*Signal`` dataclasses
    (``GrowthSignal``/``QualitySignal``/``CapitalSignal``/``TailSignal``) --
    they share the same ``observed_at``/``reliability`` shape by design.
    """
    if half_life_days is None or signal.observed_at is None:
        return signal.reliability
    observed_date = signal.observed_at
    if isinstance(observed_date, datetime.datetime):
        observed_date = observed_date.date()
    age_days = (as_of - observed_date).days
    return signal.reliability * freshness(age_days, half_life_days)


def feature_confidence_delta(
    signals: Iterable,
    *,
    not_collected_penalty: float = 0.03,
    collection_failed_penalty: float = 0.08,
    applied_bonus_scale: float = 0.02,
    bound: float = 0.20,
) -> float:
    """D-2: shared evidence-based confidence delta, used by every one of
    growth.py/quality.py/balance_sheet.py/tail_risk.py's
    ``*FeatureSet.confidence_delta`` properties (previously four
    hand-duplicated, penalty-only copies of the same loop).

    Two directions, both gated on ``runtime_enabled`` (the feature was
    configured on and universe coverage cleared the gate -- an absent
    optional source only counts against confidence when it was actually
    expected):

    - Penalty (unchanged from the original growth.py contract): a
      coverage-gated source that was expected but is
      ``NOT_COLLECTED``/``COLLECTION_FAILED`` lowers confidence.
    - Bonus (new, D-2): a signal that is ``applied`` -- i.e. real evidence
      that actually entered the state, not merely present-but-zero-effect
      (``no_change``) -- raises confidence, scaled by that signal's own
      reliability. This satisfies "confidence should rise with the amount
      and reliability of evidence that actually entered the state"
      (WP-D D-2) without reintroducing a ranking bonus for "having data":
      the arithmetic mean (``expected_moic``/``expected_cagr``) is
      unaffected by ``model_confidence`` (``scenario.py``'s scenario
      mixture is explicitly mean-preserving under confidence -- see its
      docstring); only CE CAGR/RACR's own uncertainty term and the
      scenario mixture's *dispersion* move.
    """
    delta = 0.0
    for signal in signals:
        if not signal.runtime_enabled:
            continue
        if signal.coverage_status == CoverageStatus.NOT_COLLECTED:
            delta -= not_collected_penalty
        elif signal.coverage_status == CoverageStatus.COLLECTION_FAILED:
            delta -= collection_failed_penalty
        if signal.applied:
            delta += applied_bonus_scale * signal.reliability
    return max(-bound, min(bound, delta))
