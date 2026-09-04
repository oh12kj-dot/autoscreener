"""Phase 6 tail-risk updates: customer concentration, litigation, macro regime.

Mirrors the ``quality.py``/``balance_sheet.py`` API by design: every builder
is point-in-time, coverage-gated, and returns explicit missingness. All
three signals connect to ``left_tail_extra`` only (never the mean, never
sigma uniformly) -- Issue #3 section 12's explicit requirement for
customer_concentration ("平均成長率を直接下げない") generalises to the other
two: an idiosyncratic revenue shock risk, a litigation tail risk, and a
downside-macro-sensitivity risk are all *left-tail-only* stories. Using only
``left_tail_extra`` (not ``sigma_multiplier``) is a deliberately narrower
mechanism than Phase 4's ``accounting_quality`` -- it widens only the
downside scenario's own sigma (see ``scenario.py``'s
``tail_multiplier = uncertainty.left_tail_multiplier + left_tail_extra if
name == "downside" else 1.0``), leaving the base/upside scenarios entirely
untouched, versus accounting_quality's ``sigma_multiplier`` which widens all
three. This bounds, but does NOT eliminate, the same mean-preservation side
effect Fix 2 documented for accounting_quality: the downside scenario is a
lognormal component with its own (smaller, but nonzero) probability mass
above any given threshold, so widening only its sigma still measurably
raises P(>=10x) -- confirmed directly (``left_tail_extra=0.30`` alone raised
P(10x) from 0.01303 to 0.01435 in a worked example, versus P(MOIC<1.0)
moving from 0.4267 to 0.4399), just by a smaller margin than
accounting_quality's uniform widening would produce at a comparable
magnitude. See the Phase 6 doc for the measured comparison; this docstring
does not repeat the earlier doc's overreaching "cannot raise P(target)"
framing.

M&A competing risk (``delisting_events``) is deliberately NOT implemented
here: 94/94 rows have ``event_type="unknown"`` (Phase 0 baseline), below any
defensible classification-coverage threshold, and Issue #3 section 13
explicitly prohibits treating an unclassified event as "no acquisition"
(acquisition=0). ``competing_risk.acquisition_probability`` /
``other_exit_probability`` remain ``_unsupported("phase6")`` in
state_model.py -- not fabricated as 0.0 or as a coverage-gated-off feature
that never applied (the two are different: no signal was built at all,
because the data cannot support one honestly, not because coverage happened
to fall short of a threshold).
"""

from __future__ import annotations

import datetime
import statistics
from dataclasses import asdict, dataclass, replace

from sqlalchemy.orm import Session

from autoscreener.config import ModelV5Config
from autoscreener.coverage import CoverageStatus
from autoscreener.db.models import CustomerConcentration, LitigationEvent, MacroExposureSnapshot
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY
from autoscreener.scoring.v5.growth import _coverage_status, _cutoff, _reliability
from autoscreener.scoring.v5.inputs import V5PitInput
from autoscreener.scoring.v5.reliability import decayed_reliability, feature_confidence_delta

_FEATURE_KEYS = ("customer_concentration", "litigation", "macro_regime")


@dataclass(frozen=True)
class TailSignal:
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
class TailFeatureSet:
    signals: tuple[TailSignal, ...]
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

    def excluding(self, key: str) -> TailFeatureSet:
        return TailFeatureSet(
            tuple(
                replace(signal, applied=False, status="ablated")
                if signal.key == key else signal
                for signal in self.signals
            ),
            dict(self.universe_coverage),
        )


@dataclass(frozen=True)
class TailUpdate:
    left_tail_extra: float
    applied_keys: tuple[str, ...]
    signal_effects: dict[str, dict]

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_signal(key: str, coverage_status: str, status: str | None = None) -> TailSignal:
    return TailSignal(
        key=key, status=status or coverage_status, coverage_status=coverage_status,
        runtime_enabled=False, applied=False, reliability=0.0,
        observed_at=None, value=None, evidence={},
    )


def _customer_concentration_signal(rows: list[CustomerConcentration]) -> TailSignal:
    """No coverage ledger and no coverage_status/confidence column on this
    table (K-1's ``collect_concentration.py`` only upserts a row when a
    10-K/XBRL disclosure was actually found); an absent row is treated
    conservatively as NOT_COLLECTED, matching ``dilution_capacity``'s same
    gap -- both are documented, not papered over.
    """
    if not rows:
        return _empty_signal("customer_concentration", CoverageStatus.NOT_COLLECTED, "no_disclosure_found")
    latest_period = max(row.period_end for row in rows)
    latest_rows = [row for row in rows if row.period_end == latest_period]
    total_concentration = min(1.0, max(0.0, sum(float(row.revenue_pct) for row in latest_rows)))
    reliability = 0.75 if any(row.source == "xbrl" for row in latest_rows) else 0.55
    observed_at = datetime.datetime.combine(
        max(row.collected_on for row in latest_rows), datetime.time.min, tzinfo=datetime.timezone.utc
    )
    evidence = {
        "period_end": latest_period.isoformat(), "customer_count": len(latest_rows),
        "total_disclosed_concentration": total_concentration,
        "customers": [row.customer_label for row in latest_rows],
    }
    return TailSignal(
        "customer_concentration", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        reliability, observed_at, total_concentration, evidence,
    )


def _litigation_signal(
    rows: list[LitigationEvent], *, as_of: datetime.date, config: ModelV5Config,
) -> TailSignal:
    """``litigation_events`` has no severity/amount field at all (only
    ``kind``/``title``/``detail`` text) -- there is no numeric severity to
    extract yet, a starker version of the "severity/amount coverage
    insufficient" limitation the handoff already expected. Event *count*
    within the trailing window is used as an explicitly bounded, crude
    proxy, documented as such rather than presented as a calibrated
    severity measure.
    """
    if not rows:
        return _empty_signal("litigation", CoverageStatus.NOT_COLLECTED, "no_litigation_disclosure_found")
    window_start = as_of - datetime.timedelta(days=config.tail.litigation_lookback_days)
    recent = [row for row in rows if row.event_date >= window_start]
    if not recent:
        # Scanned, nothing recent -- a legitimate "no_change", not missing.
        latest = max(rows, key=lambda row: row.collected_on)
        observed_at = datetime.datetime.combine(
            latest.collected_on, datetime.time.min, tzinfo=datetime.timezone.utc
        )
        return TailSignal(
            "litigation", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
            0.55, observed_at, 0.0,
            {"event_count_in_window": 0, "lookback_days": config.tail.litigation_lookback_days},
        )
    severity = min(1.0, len(recent) / config.tail.litigation_severity_count_cap)
    observed_at = datetime.datetime.combine(
        max(row.collected_on for row in recent), datetime.time.min, tzinfo=datetime.timezone.utc
    )
    evidence = {
        "event_count_in_window": len(recent), "lookback_days": config.tail.litigation_lookback_days,
        "severity_count_cap": config.tail.litigation_severity_count_cap,
        "kinds": sorted({row.kind for row in recent}),
    }
    return TailSignal(
        "litigation", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        0.55, observed_at, severity, evidence,
    )


def _macro_regime_signal(rows: list[MacroExposureSnapshot]) -> TailSignal:
    """``current_macro_stress x exposure``, deliberately narrowed to the
    part that is actually measurable and PIT-honest today: this repository
    already computes and stores ``downside_beta`` per (ticker, factor) --
    how much *worse* a ticker does specifically when the factor is down.
    High beta/exposure alone is not treated as bad (Issue #3 section 10:
    "金利感応度が高い=悪ではない") -- only the downside-asymmetric component
    widens the left tail, and only when the underlying FRED series
    genuinely supports point-in-time historical reconstruction
    (``fred_vintage_supported``). All ``macro_exposure_snapshots`` rows in
    this database currently have that flag False (Phase 0 baseline), so
    this signal is expected to be globally NOT_APPLICABLE today -- current
    FRED values are never used retroactively for a historical ``as_of``.
    """
    status = _coverage_status(rows)
    if not rows:
        return _empty_signal("macro_regime", status, "no_macro_exposure_data")
    valid_rows = [row for row in rows if str(row.coverage_status) == CoverageStatus.COLLECTED_WITH_DATA]
    if not valid_rows:
        return _empty_signal("macro_regime", status, status)
    vintage_rows = [
        row for row in valid_rows if bool((row.raw_payload or {}).get("fred_vintage_supported"))
    ]
    if not vintage_rows:
        # Data exists but cannot be trusted for point-in-time historical
        # reconstruction -- NOT_APPLICABLE (not COLLECTED_WITH_DATA, which
        # would misleadingly count toward the coverage gate as "usable").
        return _empty_signal(
            "macro_regime", CoverageStatus.NOT_APPLICABLE,
            "fred_vintage_unsupported_historical_backtest_prohibited",
        )
    downside_betas = [float(row.downside_beta) for row in vintage_rows if row.downside_beta is not None]
    if not downside_betas:
        return _empty_signal("macro_regime", CoverageStatus.COLLECTED_WITH_DATA, "downside_beta_unavailable")
    value = max(0.0, statistics.mean(downside_betas))
    observed_at = max(row.observed_at for row in vintage_rows)
    reliability = min(_reliability(row.confidence) for row in vintage_rows)
    evidence = {
        "downside_beta_mean": value, "factor_count": len(vintage_rows),
        "factors": sorted({row.factor for row in vintage_rows}),
    }
    return TailSignal(
        "macro_regime", "candidate", CoverageStatus.COLLECTED_WITH_DATA, False, False,
        reliability, observed_at, value, evidence,
    )


def build_tail_feature_sets(
    session: Session,
    items: list[V5PitInput],
    *,
    as_of: datetime.date,
    config: ModelV5Config,
) -> dict[int, TailFeatureSet]:
    """Load Phase 6 datasets in bulk under the same end-of-day PIT boundary."""
    ticker_ids = [item.ticker_id for item in items]
    if not ticker_ids:
        return {}
    cutoff = _cutoff(as_of)
    concentration_rows = session.query(CustomerConcentration).filter(
        CustomerConcentration.ticker_id.in_(ticker_ids), CustomerConcentration.collected_on <= as_of,
    ).order_by(CustomerConcentration.collected_on, CustomerConcentration.id).all()
    litigation_rows = session.query(LitigationEvent).filter(
        LitigationEvent.ticker_id.in_(ticker_ids), LitigationEvent.collected_on <= as_of,
    ).order_by(LitigationEvent.collected_on, LitigationEvent.id).all()
    macro_rows = session.query(MacroExposureSnapshot).filter(
        MacroExposureSnapshot.ticker_id.in_(ticker_ids), MacroExposureSnapshot.observed_at < cutoff,
    ).order_by(MacroExposureSnapshot.observed_at, MacroExposureSnapshot.id).all()

    def group(rows):
        output: dict[int, list] = {ticker_id: [] for ticker_id in ticker_ids}
        for row in rows:
            output[row.ticker_id].append(row)
        return output

    concentration_by = group(concentration_rows)
    litigation_by = group(litigation_rows)
    macro_by = group(macro_rows)

    candidates: dict[int, dict[str, TailSignal]] = {}
    for ticker_id in ticker_ids:
        candidates[ticker_id] = {
            "customer_concentration": _customer_concentration_signal(concentration_by[ticker_id]),
            "litigation": _litigation_signal(litigation_by[ticker_id], as_of=as_of, config=config),
            "macro_regime": _macro_regime_signal(macro_by[ticker_id]),
        }

    coverage = {
        key: sum(
            candidates[ticker_id][key].coverage_status == CoverageStatus.COLLECTED_WITH_DATA
            for ticker_id in ticker_ids
        ) / len(ticker_ids)
        for key in _FEATURE_KEYS
    }
    output: dict[int, TailFeatureSet] = {}
    for ticker_id in ticker_ids:
        signals = []
        for key in _FEATURE_KEYS:
            signal = candidates[ticker_id][key]
            spec = FEATURES_BY_KEY[key]
            configured = config.feature_flags.get(key, spec.default_enabled)
            runtime_enabled = configured and coverage[key] >= spec.required_coverage
            # WP-D D-3 (docs/racr_wp_d_reliability_layer_2026-09-04.md):
            # wires FeatureSpec.freshness_half_life_days (a no-op today --
            # no Phase 6 signal sets it -- but no longer dead metadata).
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
        output[ticker_id] = TailFeatureSet(tuple(signals), dict(coverage))
    return output


def apply_tail_features(
    features: TailFeatureSet, *, config: ModelV5Config, excluded_key: str | None = None,
) -> TailUpdate:
    """Map customer-concentration/litigation/macro-regime severities to a
    single bounded, additive ``left_tail_extra`` (``>= 0``; never touches
    the mean or sigma uniformly). Empty feature sets return ``0.0``,
    reproducing Phase 2-5 output exactly.
    """
    left_tail_extra = 0.0
    applied: list[str] = []
    effects: dict[str, dict] = {}

    for signal in features.signals:
        if not signal.applied or signal.key == excluded_key:
            continue
        severity = float(signal.value)
        if signal.key == "customer_concentration":
            weight, cap = config.tail.customer_concentration_weight, config.tail.customer_concentration_left_tail_max
        elif signal.key == "litigation":
            weight, cap = config.tail.litigation_weight, config.tail.litigation_left_tail_max
        elif signal.key == "macro_regime":
            weight, cap = config.tail.macro_regime_weight, config.tail.macro_regime_left_tail_max
        else:
            continue
        contribution = min(cap, weight * severity)
        left_tail_extra += contribution
        effects[signal.key] = {"severity": severity, "left_tail_extra_contribution": contribution}
        applied.append(signal.key)

    left_tail_extra = min(config.tail.max_combined_left_tail_extra, left_tail_extra)
    return TailUpdate(
        left_tail_extra=left_tail_extra, applied_keys=tuple(applied), signal_effects=effects,
    )
