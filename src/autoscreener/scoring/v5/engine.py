"""Append-only Model v5 shadow runner with Phase 2 distribution outputs."""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import uuid
from dataclasses import replace

from autoscreener.backtest.metrics import spearman
from autoscreener.config import (
    ModelV5Config,
    ObjectivesConfig,
    load_model_v5_config,
    load_objectives_config,
    load_scoring_config,
)
from autoscreener.coverage import CoverageStatus
from autoscreener.dates import utc_today
from autoscreener.db.models import ModelFeatureValue, ModelRun, ModelScore, ObjectiveScore
from autoscreener.db.session import session_scope
from autoscreener.scoring.engine import cross_section_for
from autoscreener.scoring.moic import MoicResult, compute_moic
from autoscreener.scoring.v5.balance_sheet import (
    CapitalFeatureSet,
    CapitalUpdate,
    apply_capital_features,
    build_capital_feature_sets,
)
from autoscreener.scoring.v5.distribution import scenario_distribution, unavailable_distribution
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY, feature_registry_payload
from autoscreener.scoring.v5.growth import (
    GrowthFeatureSet,
    GrowthUpdate,
    apply_growth_features,
    build_growth_feature_sets,
)
from autoscreener.scoring.v5.inputs import V5PitInput, build_v5_pit_inputs
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.path_risk import PathRiskResult, estimate_path_risk, stable_seed
from autoscreener.scoring.v5.quality import (
    QualityFeatureSet,
    QualityUpdate,
    apply_quality_features,
    build_quality_feature_sets,
)
from autoscreener.scoring.v5.reliability import base_confidence_for
from autoscreener.scoring.v5.scenario import build_scenarios
from autoscreener.scoring.v5.state_model import build_future_state
from autoscreener.scoring.v5.tail_risk import (
    TailFeatureSet,
    TailUpdate,
    apply_tail_features,
    build_tail_feature_sets,
)

# Single source of truth for the persisted contract_version: reads
# ModelV5Config.implementation_version directly (audit fix, 2026-09-03) so a
# separate module constant can never silently drift from config/model_v5.yaml.


def _code_revision_info() -> dict:
    """Best-effort ``git rev-parse HEAD`` + working-tree dirty flag.

    Audit finding (2026-09-03): two real runs shared the identical
    ``config_hash``/``implementation_version`` (``v5.phase4``) while running
    different code -- config_hash only fingerprints config/registry content,
    never the Python source, so a config-only comparison cannot tell two
    such runs apart. This is a diagnostic best-effort read, not a scoring
    input: any failure (git missing, not a repo, timeout) is recorded as an
    explicit ``null`` + reason rather than failing the run. Never backfilled
    onto pre-existing runs -- the append-only history is not rewritten.

    Standard rule from the 2026-09-03 audit (residual item 2): evidence runs
    used for a phase's doc numbers must be taken on a clean working tree
    (``dirty: False``) *after* committing that phase's implementation, not
    mid-implementation.
    """
    from autoscreener.config import PROJECT_ROOT

    def _run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = _run(["git", "rev-parse", "HEAD"])
    status = _run(["git", "status", "--porcelain"])
    return {
        "commit": commit,
        "dirty": (bool(status) if status is not None else None) if commit is not None else None,
        "reason": None if commit is not None else "git_unavailable_or_not_a_repo",
    }


def v5_config_hash(model_config: ModelV5Config, objectives_config: ObjectivesConfig) -> str:
    canonical = json.dumps(
        {
            "model": model_config.model_dump(),
            "objectives": objectives_config.model_dump(),
            "feature_registry": feature_registry_payload(model_config.feature_flags),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _feature_payload(
    item: V5PitInput,
    registry: list[dict],
    growth_features: GrowthFeatureSet,
    quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet,
    tail_features: TailFeatureSet,
    growth_update: GrowthUpdate | None,
    quality_update: QualityUpdate | None,
    capital_update: CapitalUpdate | None,
    tail_update: TailUpdate | None,
    ablation: dict,
    core_evidence=None,
) -> dict:
    payload = {
        "registry_version": "phase6",
        "pit_evidence": item.evidence(),
        "contracts": [
            {
                "key": feature["key"],
                "enabled": feature["enabled"],
                "target_state": feature["target_state"],
                "historical_backtest_supported": feature["historical_backtest_supported"],
            }
            for feature in registry
        ],
        "growth_features": growth_features.to_dict(),
        "quality_features": quality_features.to_dict(),
        "capital_features": capital_features.to_dict(),
        "tail_features": tail_features.to_dict(),
        "ablation": ablation,
        # WP-D D-1 (docs/racr_wp_d_reliability_layer_2026-09-04.md): the
        # per-ticker audit §7.3 factors behind `model_confidence`, so a
        # score's confidence is not just a number but reconstructible from
        # the evidence that produced it.
        "core_evidence_reliability": core_evidence.to_dict() if core_evidence is not None else None,
    }
    if growth_update is not None:
        payload["growth_update"] = growth_update.to_dict()
    if quality_update is not None:
        payload["quality_update"] = quality_update.to_dict()
    if capital_update is not None:
        payload["capital_update"] = capital_update.to_dict()
    if tail_update is not None:
        payload["tail_update"] = tail_update.to_dict()
    return payload


def _feature_value_rows(
    run_id: uuid.UUID,
    ticker_id: int,
    core_evidence,
    signals,
) -> list[ModelFeatureValue]:
    """D-4 (docs/racr_wp_d_reliability_layer_2026-09-04.md; audit P2): one
    row per (run, ticker, feature) -- value, source, availability,
    reliability, and missing reason -- for every optional growth/quality/
    capital/tail signal evaluated this run, plus the two always-present
    base features (financial statements, price history) derived from
    ``core_evidence``. Kept alongside ``model_scores.features`` (unchanged,
    still the full nested JSONB blob) rather than replacing it: this is a
    queryable, indexable projection of the same evidence, not a second
    source of truth.
    """
    rows: list[ModelFeatureValue] = []
    if core_evidence is not None:
        rows.append(ModelFeatureValue(
            run_id=run_id, ticker_id=ticker_id, feature_key="base_financial_statements",
            value=float(core_evidence.annual_periods), source="raw_snapshots",
            coverage_status=(
                CoverageStatus.COLLECTED_WITH_DATA if core_evidence.annual_periods > 0
                else CoverageStatus.NOT_COLLECTED
            ),
            status="seed", applied=True, reliability=core_evidence.value,
            missing_reason=None if core_evidence.annual_periods > 0 else "no_annual_periods",
            observed_at=None, evidence=core_evidence.to_dict(),
        ))
        rows.append(ModelFeatureValue(
            run_id=run_id, ticker_id=ticker_id, feature_key="price_history",
            value=float(core_evidence.price_row_count), source="price_snapshots",
            coverage_status=(
                CoverageStatus.COLLECTED_WITH_DATA if core_evidence.price_row_count > 0
                else CoverageStatus.NOT_COLLECTED
            ),
            status="seed", applied=True, reliability=core_evidence.q_sample,
            missing_reason=None if core_evidence.price_row_count > 0 else "no_price_history",
            observed_at=None, evidence={"price_row_count": core_evidence.price_row_count},
        ))
    for signal in signals:
        rows.append(ModelFeatureValue(
            run_id=run_id, ticker_id=ticker_id, feature_key=signal.key,
            value=float(signal.value) if signal.value is not None else None,
            source=FEATURES_BY_KEY[signal.key].source,
            coverage_status=str(signal.coverage_status),
            status=signal.status, applied=signal.applied, reliability=signal.reliability,
            missing_reason=None if signal.applied else signal.status,
            observed_at=signal.observed_at, evidence=signal.evidence,
        ))
    return rows


def _score_warnings(
    item: V5PitInput, has_distribution: bool,
    growth_features: GrowthFeatureSet, quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet, tail_features: TailFeatureSet,
) -> list[str]:
    warnings = ["phase6_state_updates_shadow_only", "not_for_production"]
    if item.raw_snapshot_id is not None:
        warnings.append("financial_statement_pit_is_approximate")
    if item.input_status == "not_collected":
        warnings.append("raw_snapshot_not_available_as_of")
    elif not has_distribution:
        warnings.append("distribution_unavailable")
    disabled = [
        signal.key
        for signal in (
            *growth_features.signals, *quality_features.signals,
            *capital_features.signals, *tail_features.signals,
        )
        if signal.status == "runtime_disabled_low_coverage"
    ]
    if disabled:
        warnings.append("coverage_gated_features:" + ",".join(sorted(disabled)))
    return warnings


def _distribution_for(
    result: MoicResult,
    *,
    growth_features: GrowthFeatureSet,
    quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet,
    tail_features: TailFeatureSet,
    base_confidence: float,
    model_config: ModelV5Config,
    path_risk_result: PathRiskResult | None = None,
) -> tuple[GrowthUpdate, QualityUpdate, CapitalUpdate, TailUpdate, float, dict]:
    """Compute one (growth_update, quality_update, capital_update, tail_update,
    confidence, distribution) tuple.

    The single place Phase 3-6 state updates are combined into a
    distribution, so the leave-one-out ablation loop below can call this
    with any signal excluded from any feature set and get a directly
    comparable counterfactual, instead of duplicating the combination logic
    per phase (handoff 4.8: do not copy-paste the Phase 3 ablation loop).

    ``path_risk_result`` (WP-F1) is computed once per ticker by the caller
    (from realized price history, independent of every growth/quality/
    capital/tail feature) and simply passed through to
    ``scenario_distribution`` here -- ablation counterfactuals reuse the
    same value rather than recomputing it, since none of those features
    can change a ticker's own price history.
    """
    confidence = max(
        0.0,
        min(
            1.0,
            base_confidence + growth_features.confidence_delta
            + quality_features.confidence_delta + capital_features.confidence_delta
            + tail_features.confidence_delta,
        ),
    )
    growth_update = apply_growth_features(result, growth_features, config=model_config)
    quality_update = apply_quality_features(
        result, quality_features, config=model_config, growth_update=growth_update,
    )
    capital_update = apply_capital_features(
        result, capital_features, config=model_config, quality_update=quality_update,
    )
    tail_update = apply_tail_features(tail_features, config=model_config)
    mean_multiplier = (
        growth_update.revenue_multiple_ratio * quality_update.mean_multiplier
        * capital_update.mean_multiplier
    )
    # Both Phase 4 accounting_quality and Phase 6 tail-risk widen only the
    # left tail; summed here (each already bounded by its own config cap)
    # rather than allowing either module to know about the other.
    left_tail_extra = quality_update.left_tail_extra + tail_update.left_tail_extra
    scenarios = build_scenarios(
        result, confidence=confidence, config=model_config,
        conditional_mean_multiplier=mean_multiplier,
        sigma_multiplier=quality_update.sigma_multiplier,
        left_tail_extra=left_tail_extra,
        survival_multiplier=capital_update.survival_multiplier,
    )
    distribution = scenario_distribution(
        scenarios, horizon_years=model_config.target_horizon_years,
        target_moic=model_config.target_moic, confidence=confidence,
        sigma_multiplier=quality_update.sigma_multiplier,
        left_tail_extra=left_tail_extra,
        path_risk=path_risk_result,
    )
    return growth_update, quality_update, capital_update, tail_update, confidence, distribution


def _ablate(
    key: str,
    *,
    result: MoicResult,
    growth_features: GrowthFeatureSet,
    quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet,
    tail_features: TailFeatureSet,
    base_confidence: float,
    model_config: ModelV5Config,
    full_growth_update: GrowthUpdate,
    full_quality_update: QualityUpdate,
    full_capital_update: CapitalUpdate,
    full_tail_update: TailUpdate,
    full_confidence: float,
    full_distribution: dict,
    path_risk_result: PathRiskResult | None = None,
) -> dict:
    """Leave-one-feature-out counterfactual, any growth/quality/capital/tail key."""
    is_growth_key = any(signal.key == key for signal in growth_features.signals)
    is_quality_key = any(signal.key == key for signal in quality_features.signals)
    is_capital_key = any(signal.key == key for signal in capital_features.signals)
    without_growth = growth_features.excluding(key) if is_growth_key else growth_features
    without_quality = quality_features.excluding(key) if is_quality_key else quality_features
    without_capital = capital_features.excluding(key) if is_capital_key else capital_features
    without_tail = (
        tail_features.excluding(key)
        if not is_growth_key and not is_quality_key and not is_capital_key
        else tail_features
    )
    (
        without_growth_update, without_quality_update, without_capital_update,
        without_tail_update, without_confidence, without_distribution,
    ) = _distribution_for(
        result, growth_features=without_growth, quality_features=without_quality,
        capital_features=without_capital, tail_features=without_tail,
        base_confidence=base_confidence, model_config=model_config,
        path_risk_result=path_risk_result,
    )
    full_duration = full_growth_update.updated_duration_years * full_quality_update.duration_multiplier
    without_duration = without_growth_update.updated_duration_years * without_quality_update.duration_multiplier
    full_mean = (
        full_growth_update.revenue_multiple_ratio * full_quality_update.mean_multiplier
        * full_capital_update.mean_multiplier
    )
    without_mean = (
        without_growth_update.revenue_multiple_ratio * without_quality_update.mean_multiplier
        * without_capital_update.mean_multiplier
    )
    full_left_tail = full_quality_update.left_tail_extra + full_tail_update.left_tail_extra
    without_left_tail = without_quality_update.left_tail_extra + without_tail_update.left_tail_extra
    return {
        "status": "computed",
        "state_shift": {
            "initial_growth_rate": (
                full_growth_update.updated_initial_rate - without_growth_update.updated_initial_rate
            ),
            "growth_duration_years": full_duration - without_duration,
            "revenue_multiple_ratio": full_mean - without_mean,
            "sigma_multiplier": full_quality_update.sigma_multiplier - without_quality_update.sigma_multiplier,
            "left_tail_extra": full_left_tail - without_left_tail,
            "survival_multiplier": full_capital_update.survival_multiplier - without_capital_update.survival_multiplier,
            "model_confidence": full_confidence - without_confidence,
        },
        "scenario_impact": {
            "p_target": full_distribution["p_target"] - without_distribution["p_target"],
            "expected_cagr": full_distribution["expected_cagr"] - without_distribution["expected_cagr"],
            "p_moic_below_1_0": (
                full_distribution["p_moic_below_1_0"] - without_distribution["p_moic_below_1_0"]
            ),
        },
        "without_feature": {
            "p_target": without_distribution["p_target"],
            "expected_cagr": without_distribution["expected_cagr"],
            "p_moic_below_1_0": without_distribution["p_moic_below_1_0"],
            "model_confidence": without_confidence,
        },
    }


def _reconcile_quality_features(
    quality_features: QualityFeatureSet, quality_update: QualityUpdate
) -> QualityFeatureSet:
    """Downgrade a persisted signal to ``no_change`` when it had zero effect.

    ``build_quality_feature_sets`` flags a signal "applied" from its raw
    value alone (coverage/reliability/nonzero-value gates), before any
    ticker-specific result exists. ``apply_quality_features`` can determine,
    with the real result in hand, that a nominally-applied signal (e.g.
    incremental_roic when growth is not elevated) produced no measurable
    effect this run. Without this reconciliation the persisted
    ``quality_features`` payload would keep saying ``applied: true`` for a
    signal that moved nothing -- exactly the gap the 2026-09-03 audit found
    by cross-referencing ablation output against the "applied" flag.
    """
    if not quality_update.no_effect_keys:
        return quality_features
    no_effect = set(quality_update.no_effect_keys)
    return QualityFeatureSet(
        tuple(
            replace(signal, applied=False, status="no_change")
            if signal.key in no_effect else signal
            for signal in quality_features.signals
        ),
        dict(quality_features.universe_coverage),
    )


def _reconcile_capital_features(
    capital_features: CapitalFeatureSet, capital_update: CapitalUpdate
) -> CapitalFeatureSet:
    """Same reconciliation as ``_reconcile_quality_features``, for capital
    signals (currently only ``future_dilution_capacity`` can produce a
    ``no_effect_keys`` entry, when the shared anti-triple-counting budget
    was already exhausted by Phase 4)."""
    if not capital_update.no_effect_keys:
        return capital_features
    no_effect = set(capital_update.no_effect_keys)
    return CapitalFeatureSet(
        tuple(
            replace(signal, applied=False, status="no_change")
            if signal.key in no_effect else signal
            for signal in capital_features.signals
        ),
        dict(capital_features.universe_coverage),
    )


# WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md B2-1; deferred from WP-B's
# own B-3 acceptance condition, docs/racr_wp_b_output_contract_2026-09-04.md
# §5 "実施できなかった診断出力"): per-run objective diagnostics that would
# have caught the exact defect this WP fixes automatically -- the first real
# run measured Spearman(RACR, ce_cagr) == 1.0000000000 for 1,155/1,155
# tickers because every risk term turned out to be a universe-wide constant
# (docs/racr_shadow_run_diagnostic_2026-09-04.md). These are pure functions
# over plain dict/list/set inputs (never ORM rows or a session), so they are
# unit-testable without a DB, matching this module's existing style (e.g.
# ``_ablate``/``_distribution_for`` also take plain values).

_DIAGNOSTIC_DISTRIBUTION_FIELDS = (
    "ce_cagr",
    "expected_cagr",
    "median_cagr",
    "survival_probability",
    "model_confidence",
    "expected_shortfall_10pct_log",
    "expected_shortfall_10pct_log_given_survival",
    "p_target",
    # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): tracked here so the
    # existing distinct-value/constant-field diagnostic automatically
    # covers path risk too -- this is the acceptance-criteria measurement
    # ("distinct-value count and quantiles of expected_max_drawdown across
    # the scored universe") persisted per run instead of only ever computed
    # by hand.
    "expected_max_drawdown",
    "expected_drawdown_excess_35",
)


def _numeric(value: object) -> float | None:
    """``float`` for a non-bool int/float, else ``None``.

    ``bool`` is a subclass of ``int`` in Python; a boolean flag reaching a
    numeric comparison here would otherwise be silently treated as 0/1 and
    could report a spurious "distinct value count" of 1 -- excluded
    explicitly rather than relying on every caller to remember.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pairwise_objective_spearman(
    objective_values: dict[str, dict[int, float]],
    ce_cagr_by_ticker: dict[int, float],
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Pairwise Spearman between every enabled objective, and each
    objective vs ``ce_cagr``. Returns ``(pairwise, vs_ce_cagr, warnings)``.

    A pair is only reported when at least 3 tickers have a non-null score
    on both sides -- ``spearman`` itself already returns 0.0 below that,
    but a *reported* 0.0 here would misleadingly read as "measured, no
    correlation" instead of "not enough overlap to measure".
    """
    pairwise: dict[str, float] = {}
    warnings: list[str] = []
    names = sorted(objective_values)
    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            common = sorted(set(objective_values[name_a]) & set(objective_values[name_b]))
            if len(common) < 3:
                continue
            xs = [objective_values[name_a][t] for t in common]
            ys = [objective_values[name_b][t] for t in common]
            rho = spearman(xs, ys)
            pairwise[f"{name_a}__vs__{name_b}"] = rho
            # The old `risk_adjusted` sat at Spearman 0.992 against
            # `expected_return` and was effectively a duplicate objective
            # (docs/racr_shadow_run_diagnostic_2026-09-04.md §2) -- ~0.99 is
            # the threshold past which two objectives stop carrying
            # independent ranking information.
            if abs(rho) > 0.99:
                warnings.append(
                    f"objective_duplicate_risk:{name_a}_vs_{name_b}_spearman_{rho:.4f}"
                )
    vs_ce_cagr: dict[str, float] = {}
    for name, values in objective_values.items():
        common = sorted(set(values) & set(ce_cagr_by_ticker))
        if len(common) < 3:
            continue
        xs = [values[t] for t in common]
        ys = [ce_cagr_by_ticker[t] for t in common]
        vs_ce_cagr[name] = spearman(xs, ys)
    return pairwise, vs_ce_cagr, warnings


def _top20_overlap_vs_expected_return(
    objective_ranks: dict[str, dict[int, int]],
) -> dict[str, int]:
    """Count of tickers in both an objective's top 20 by rank and
    ``expected_return``'s top 20, for every other enabled objective."""
    expected_return_ranks = objective_ranks.get("expected_return", {})
    expected_top20 = {ticker for ticker, rank in expected_return_ranks.items() if rank <= 20}
    if not expected_top20:
        return {}
    overlap: dict[str, int] = {}
    for name, ranks in objective_ranks.items():
        if name == "expected_return":
            continue
        top20 = {ticker for ticker, rank in ranks.items() if rank <= 20}
        overlap[name] = len(top20 & expected_top20)
    return overlap


def _constant_explanation_terms(
    objective_explanations: dict[str, list[dict]],
) -> tuple[dict[str, list[str]], list[str]]:
    """For each enabled objective's ``explanation``, the numeric-valued keys
    that took exactly one distinct value across every scored ticker.

    This is the exact diagnostic that would have caught
    ``risk_adjusted_compounding``'s first real run automatically: every one
    of its risk terms (the old ``tail_loss_10``, and ``model_confidence``
    feeding ``model_uncertainty``) was a universe-wide constant
    (docs/racr_shadow_run_diagnostic_2026-09-04.md). Values are rounded to
    9 decimal places before comparing so float-representation noise between
    independently-but-equally computed values does not manufacture false
    "distinct" values; the rounding is intentionally tight enough that it
    cannot launder a real (if small) cross-sectional difference into a
    false "constant".

    Flags every qualifying key, including fixed policy constants that are
    constant *by design* (lambda coefficients, ``ce_cagr_failure_floor``,
    the zeroed placeholder terms for unavailable statistics) --
    distinguishing "expected constant" from "defect" is left to the run's
    own docs/review, per this WP's instruction that a constant term "must
    never again be found by hand": false positives on known constants are
    an acceptable cost of never again missing a real one.
    """
    constant_terms: dict[str, list[str]] = {}
    warnings: list[str] = []
    for name, explanations in objective_explanations.items():
        if len(explanations) < 2:
            continue
        numeric_keys: set[str] = set()
        for explanation in explanations:
            for key, value in explanation.items():
                if _numeric(value) is not None:
                    numeric_keys.add(key)
        for key in sorted(numeric_keys):
            distinct = {
                round(_numeric(explanation[key]), 9)
                for explanation in explanations
                if key in explanation and _numeric(explanation[key]) is not None
            }
            if len(distinct) == 1:
                constant_terms.setdefault(name, []).append(key)
                warnings.append(
                    f"objective_constant_term:{name}.{key}={next(iter(distinct))}"
                )
    return constant_terms, warnings


def _distribution_field_diagnostics(
    distribution_field_values: dict[str, set[float]],
    distribution_field_counts: dict[str, int],
) -> tuple[dict[str, int], list[str], list[str]]:
    """Distinct-value counts for the key distribution fields, across every
    ticker with an *available* distribution this run.

    Mirrors ``_constant_explanation_terms`` but for the shared distribution
    contract rather than any one objective's explanation -- this is what
    would have caught ``model_confidence`` sitting at the single value 0.5
    for the entire 2026-09-04 universe
    (docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2).
    """
    distinct_counts = {
        field: len(values) for field, values in distribution_field_values.items()
    }
    constant_fields = [
        field
        for field, values in distribution_field_values.items()
        if distribution_field_counts.get(field, 0) >= 2 and len(values) == 1
    ]
    warnings = [
        f"distribution_constant_field:{field}={next(iter(distribution_field_values[field]))}"
        for field in constant_fields
    ]
    return distinct_counts, constant_fields, warnings


def _value_summary(values: list[float]) -> dict:
    """WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md): distinct-value
    count and min/p25/median/p75/max for a plain list of floats -- the
    acceptance-criteria shape this WP's doc has to report for
    ``model_confidence`` and per-signal reliability. A pure function over a
    plain list, matching this module's existing diagnostic-function style.
    """
    if not values:
        return {"n": 0, "distinct": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None}
    ordered = sorted(values)

    def _percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        k = (len(ordered) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
        if lo == hi:
            return ordered[lo]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    return {
        "n": len(ordered),
        "distinct": len({round(v, 9) for v in ordered}),
        "min": ordered[0],
        "p25": _percentile(0.25),
        "median": _percentile(0.5),
        "p75": _percentile(0.75),
        "max": ordered[-1],
    }


def run_v5_shadow(
    as_of: datetime.date | None = None,
    *,
    model_config: ModelV5Config | None = None,
    objectives_config: ObjectivesConfig | None = None,
) -> dict[str, int | str]:
    """Run v5 without reading or mutating the v4 ``scores`` table."""

    as_of = as_of or utc_today()
    model_config = model_config or load_model_v5_config()
    objectives_config = objectives_config or load_objectives_config()
    if not model_config.enabled:
        return {"status": "skipped", "reason": "disabled_by_config", "population": 0,
                "base_distributions": 0, "empty_distributions": 0}
    if model_config.mode != "shadow":
        raise ValueError("v5 shadow runner requires mode=shadow")

    registry = feature_registry_payload(model_config.feature_flags)
    current_hash = v5_config_hash(model_config, objectives_config)
    code_revision = _code_revision_info()
    run_id = uuid.uuid4()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    with session_scope() as session:
        session.add(ModelRun(
            id=run_id,
            model_version=model_config.model_version,
            config_hash=current_hash,
            as_of=as_of,
            mode="shadow",
            status="running",
            population_count=0,
            started_at=started_at,
            # code_revision is recorded here (not only at completion) so it
            # survives even a run that later fails -- config_hash alone
            # cannot distinguish two runs on different code with identical
            # config/registry content (2026-09-03 audit finding).
            metrics={"code_revision": code_revision},
            warnings=["phase6_tail_macro_state_updates", "v4_champion_unchanged"],
        ))

    try:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            if run is None:
                raise RuntimeError(f"ModelRun {run_id} disappeared")
            items = build_v5_pit_inputs(session, as_of=as_of)
            ready = {item.ticker_id: item.moic_inputs for item in items if item.moic_inputs is not None}
            scoring_config = load_scoring_config().model_copy(update={
                "horizon_years": model_config.target_horizon_years,
                "target_moic": model_config.target_moic,
            })
            cross_section = cross_section_for(ready, scoring_config)
            growth_feature_sets = build_growth_feature_sets(
                session, items, as_of=as_of, config=model_config
            )
            quality_feature_sets = build_quality_feature_sets(
                session, items, as_of=as_of, config=model_config
            )
            capital_feature_sets = build_capital_feature_sets(
                session, items, as_of=as_of, config=model_config
            )
            tail_feature_sets = build_tail_feature_sets(
                session, items, as_of=as_of, config=model_config
            )
            base_count = 0
            empty_count = 0
            objective_rows: dict[str, list[ObjectiveScore]] = {}
            applied_counts: dict[str, int] = {}
            ablation_count = 0
            total_signal_slots = 0
            # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md B2-1):
            # accumulated across the per-ticker loop below, consumed after
            # ranking is assigned to build ``objective_diagnostics``.
            ce_cagr_by_ticker: dict[int, float] = {}
            # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): mirrors
            # ce_cagr_by_ticker above, for the same reason.
            expected_max_drawdown_by_ticker: dict[int, float] = {}
            distribution_field_values: dict[str, set[float]] = {
                field: set() for field in _DIAGNOSTIC_DISTRIBUTION_FIELDS
            }
            distribution_field_counts: dict[str, int] = {
                field: 0 for field in _DIAGNOSTIC_DISTRIBUTION_FIELDS
            }
            # WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md):
            # per-ticker core-evidence reliability values, for the same
            # distinct-value/quantile reporting `_DIAGNOSTIC_DISTRIBUTION_FIELDS`
            # gets -- this is what would have caught `model_confidence`'s
            # 0.5-for-everyone defect at the source, not just downstream.
            core_evidence_values: list[float] = []
            for item in items:
                result = None
                if item.moic_inputs is not None:
                    result = compute_moic(
                        item.moic_inputs, cross_section, scoring_config,
                        enforce_min_expected_moic=False,
                    )
                has_distribution = result is not None
                base_count += int(has_distribution)
                empty_count += int(not has_distribution)
                growth_features = growth_feature_sets[item.ticker_id]
                quality_features = quality_feature_sets[item.ticker_id]
                capital_features = capital_feature_sets[item.ticker_id]
                tail_features = tail_feature_sets[item.ticker_id]
                all_signals = (
                    *growth_features.signals, *quality_features.signals,
                    *capital_features.signals, *tail_features.signals,
                )
                total_signal_slots += len(all_signals)
                # WP-D D-1 (docs/racr_wp_d_reliability_layer_2026-09-04.md):
                # replaces the flat `ready_input_confidence` constant (0.5
                # for every ticker -- root cause diagnosed in
                # docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2) with a
                # real per-ticker evidence reliability. `core_evidence` is
                # `None` exactly when `has_distribution` is False.
                base_confidence, core_evidence = base_confidence_for(
                    item, as_of=as_of, config=model_config, has_distribution=has_distribution,
                )
                ablation: dict[str, dict] = {
                    signal.key: {
                        "status": "not_computed",
                        "reason": (
                            "distribution_unavailable"
                            if not has_distribution else signal.status
                        ),
                    }
                    for signal in all_signals
                }
                growth_update: GrowthUpdate | None = None
                quality_update: QualityUpdate | None = None
                capital_update: CapitalUpdate | None = None
                tail_update: TailUpdate | None = None
                if result is None:
                    confidence = base_confidence
                    distribution = unavailable_distribution(
                        target_moic=model_config.target_moic, confidence=confidence
                    )
                    path_risk_result = None
                else:
                    # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md):
                    # computed once per ticker, strictly from this
                    # ticker's own PIT-filtered `price_snapshots` rows
                    # (``item.price_observations``, already filtered to
                    # ``trade_date <= as_of`` by ``build_v5_pit_inputs``) --
                    # never from ``result`` (the V4 seed's mu/sigma/
                    # survival) or from any growth/quality/capital/tail
                    # feature, so it is independent of every other risk
                    # term in the distribution/RACR by construction.
                    path_risk_result = estimate_path_risk(
                        item.price_observations,
                        as_of=as_of,
                        horizon_years=model_config.target_horizon_years,
                        simulations=model_config.path_risk.simulations,
                        block_weeks=model_config.path_risk.block_weeks,
                        seed=stable_seed(item.ticker_id, as_of),
                    )
                    (
                        growth_update, quality_update, capital_update, tail_update,
                        confidence, distribution,
                    ) = _distribution_for(
                        result, growth_features=growth_features, quality_features=quality_features,
                        capital_features=capital_features, tail_features=tail_features,
                        base_confidence=base_confidence, model_config=model_config,
                        path_risk_result=path_risk_result,
                    )
                    if quality_update.no_effect_keys:
                        quality_features = _reconcile_quality_features(quality_features, quality_update)
                        for key in quality_update.no_effect_keys:
                            ablation[key] = {
                                "status": "not_computed",
                                "reason": "no_change_zero_growth_or_reduction",
                            }
                    if capital_update.no_effect_keys:
                        capital_features = _reconcile_capital_features(capital_features, capital_update)
                        for key in capital_update.no_effect_keys:
                            ablation[key] = {
                                "status": "not_computed",
                                "reason": "no_change_zero_overhang_or_budget_exhausted",
                            }
                    applied_keys = (
                        *growth_update.applied_keys, *quality_update.applied_keys,
                        *capital_update.applied_keys, *tail_update.applied_keys,
                    )
                    ablation_enabled = (
                        model_config.growth.ablation_enabled
                        and model_config.quality.ablation_enabled
                        and model_config.capital.ablation_enabled
                        and model_config.tail.ablation_enabled
                    )
                    for key in applied_keys:
                        applied_counts[key] = applied_counts.get(key, 0) + 1
                        if not ablation_enabled:
                            continue
                        ablation[key] = _ablate(
                            key, result=result, growth_features=growth_features,
                            quality_features=quality_features, capital_features=capital_features,
                            tail_features=tail_features, base_confidence=base_confidence,
                            model_config=model_config, full_growth_update=growth_update,
                            full_quality_update=quality_update, full_capital_update=capital_update,
                            full_tail_update=tail_update, full_confidence=confidence,
                            full_distribution=distribution, path_risk_result=path_risk_result,
                        )
                        ablation_count += 1
                # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md B2-1):
                # accumulate the cross-sectional inputs `evaluate_objectives`
                # cannot see on its own (it only ever gets one ticker's
                # distribution at a time) -- ``ce_cagr`` per ticker for the
                # "vs ce_cagr" Spearman, and the key distribution fields'
                # distinct-value sets for constant-term detection.
                if distribution.get("status") == "available":
                    if distribution.get("ce_cagr") is not None:
                        ce_cagr_by_ticker[item.ticker_id] = distribution["ce_cagr"]
                    # WP-F1: per-ticker expected_max_drawdown, so the run's
                    # own diagnostics can report Spearman(expected_max_drawdown,
                    # ce_cagr) -- the acceptance-criteria measurement that
                    # distinguishes "real independent risk signal" from "the
                    # V4-seed-collinearity failure the prior three WPs hit"
                    # (docs/racr_shadow_run_diagnostic_2026-09-04.md §6).
                    if distribution.get("expected_max_drawdown") is not None:
                        expected_max_drawdown_by_ticker[item.ticker_id] = (
                            distribution["expected_max_drawdown"]
                        )
                    for field in _DIAGNOSTIC_DISTRIBUTION_FIELDS:
                        numeric_value = _numeric(distribution.get(field))
                        if numeric_value is not None:
                            distribution_field_values[field].add(round(numeric_value, 9))
                            distribution_field_counts[field] += 1
                future_state = build_future_state(
                    result,
                    item.moic_inputs,
                    horizon_years=model_config.target_horizon_years,
                    confidence=confidence,
                    growth_update=growth_update,
                    quality_update=quality_update,
                    capital_update=capital_update,
                    tail_update=tail_update,
                    contract_version=model_config.implementation_version,
                )
                session.add(ModelScore(
                    run_id=run_id,
                    ticker_id=item.ticker_id,
                    target_horizon_years=model_config.target_horizon_years,
                    target_moic=model_config.target_moic,
                    distribution=distribution,
                    states=future_state.to_dict(),
                    features=_feature_payload(
                        item, registry, growth_features, quality_features, capital_features,
                        tail_features, growth_update, quality_update, capital_update, tail_update,
                        ablation, core_evidence=core_evidence,
                    ),
                    confidence=confidence,
                    warnings=_score_warnings(
                        item, has_distribution, growth_features, quality_features,
                        capital_features, tail_features,
                    ),
                ))
                # D-4 (docs/racr_wp_d_reliability_layer_2026-09-04.md; audit
                # P2): the queryable per-feature layer, built from the same
                # (possibly reconciled) feature sets just persisted above.
                final_signals = (
                    *growth_features.signals, *quality_features.signals,
                    *capital_features.signals, *tail_features.signals,
                )
                for feature_row in _feature_value_rows(
                    run_id, item.ticker_id, core_evidence, final_signals,
                ):
                    session.add(feature_row)
                if core_evidence is not None:
                    core_evidence_values.append(core_evidence.value)
                objective_results = evaluate_objectives(
                    distribution,
                    objectives_config,
                    horizon_years=model_config.target_horizon_years,
                )
                for objective, objective_result in objective_results.items():
                    row = ObjectiveScore(
                        run_id=run_id,
                        ticker_id=item.ticker_id,
                        objective=objective,
                        score_value=objective_result.score_value,
                        rank=None,
                        explanation={
                            "status": objective_result.status,
                            **objective_result.explanation,
                        },
                    )
                    session.add(row)
                    objective_rows.setdefault(objective, []).append(row)

            # Objective ranking is independent from the immutable distribution.
            # Null/unavailable scores deliberately remain unranked.
            for rows in objective_rows.values():
                rankable = [row for row in rows if row.score_value is not None]
                rankable.sort(
                    key=lambda row: (-float(row.score_value), row.ticker_id)
                )
                for rank, row in enumerate(rankable, start=1):
                    row.rank = rank

            # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md B2-1): computed
            # once, after ranking, from the plain accumulators built during
            # the loop above -- never touches the DB itself (pure functions).
            objective_values: dict[str, dict[int, float]] = {
                name: {
                    row.ticker_id: float(row.score_value)
                    for row in rows if row.score_value is not None
                }
                for name, rows in objective_rows.items()
            }
            objective_ranks: dict[str, dict[int, int]] = {
                name: {
                    row.ticker_id: row.rank
                    for row in rows if row.rank is not None
                }
                for name, rows in objective_rows.items()
            }
            objective_explanations: dict[str, list[dict]] = {
                name: [
                    row.explanation for row in rows
                    if row.explanation.get("status") == "available"
                ]
                for name, rows in objective_rows.items()
            }
            pairwise_spearman, spearman_vs_ce_cagr, spearman_warnings = (
                _pairwise_objective_spearman(objective_values, ce_cagr_by_ticker)
            )
            top20_overlap = _top20_overlap_vs_expected_return(objective_ranks)
            constant_explanation_terms, constant_term_warnings = (
                _constant_explanation_terms(objective_explanations)
            )
            distribution_distinct_counts, constant_distribution_fields, distribution_warnings = (
                _distribution_field_diagnostics(distribution_field_values, distribution_field_counts)
            )
            objective_diagnostics = {
                "pairwise_spearman": pairwise_spearman,
                "spearman_vs_ce_cagr": spearman_vs_ce_cagr,
                "top20_overlap_vs_expected_return": top20_overlap,
                "constant_explanation_terms": constant_explanation_terms,
                "distribution_distinct_value_counts": distribution_distinct_counts,
                "distribution_constant_fields": constant_distribution_fields,
            }
            # WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md):
            # distinct-value/quantile summary for model_confidence itself
            # (also already covered by distribution_distinct_value_counts
            # above, repeated here with quantiles) and the core-evidence
            # reliability feeding it -- the measured numbers the WP-D doc's
            # acceptance criteria require, persisted per run instead of only
            # ever computed by hand.
            reliability_diagnostics = {
                "model_confidence": _value_summary(sorted(distribution_field_values["model_confidence"])),
                "core_evidence_reliability": _value_summary(core_evidence_values),
            }
            # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): the acceptance
            # criteria this WP's doc has to report -- distinct-value count
            # and quantiles of expected_max_drawdown across the scored
            # universe, and its Spearman against ce_cagr (the number that
            # tells whether this is a real independent risk signal or a
            # V4-seed-collinearity repeat of WP-B2/WP-D's failure).
            common_mdd_tickers = sorted(set(expected_max_drawdown_by_ticker) & set(ce_cagr_by_ticker))
            path_risk_diagnostics = {
                "expected_max_drawdown": _value_summary(
                    sorted(expected_max_drawdown_by_ticker.values())
                ),
                "spearman_expected_max_drawdown_vs_ce_cagr": (
                    spearman(
                        [expected_max_drawdown_by_ticker[t] for t in common_mdd_tickers],
                        [ce_cagr_by_ticker[t] for t in common_mdd_tickers],
                    )
                    if len(common_mdd_tickers) >= 3 else None
                ),
                "available_count": len(expected_max_drawdown_by_ticker),
            }
            diagnostic_warnings = [
                *spearman_warnings, *constant_term_warnings, *distribution_warnings,
            ]
            if diagnostic_warnings:
                run.warnings = [*(run.warnings or []), *diagnostic_warnings]

            run.population_count = len(items)
            run.status = "succeeded"
            run.finished_at = datetime.datetime.now(datetime.timezone.utc)
            feature_universe_coverage: dict[str, float] = {}
            if growth_feature_sets:
                feature_universe_coverage.update(next(iter(growth_feature_sets.values())).universe_coverage)
            if quality_feature_sets:
                feature_universe_coverage.update(next(iter(quality_feature_sets.values())).universe_coverage)
            if capital_feature_sets:
                feature_universe_coverage.update(next(iter(capital_feature_sets.values())).universe_coverage)
            if tail_feature_sets:
                feature_universe_coverage.update(next(iter(tail_feature_sets.values())).universe_coverage)
            run.metrics = {
                # code_revision was already stored at creation; carried
                # forward here rather than lost when this dict replaces
                # run.metrics wholesale on success.
                "code_revision": code_revision,
                "input_ready": len(ready),
                "base_distributions": base_count,
                "phase2_distributions": base_count,
                "phase3_distributions": base_count,
                "phase4_distributions": base_count,
                "phase5_distributions": base_count,
                "phase6_distributions": base_count,
                "empty_distributions": empty_count,
                "objective_scores": sum(len(rows) for rows in objective_rows.values()),
                "enabled_objectives": sorted(objective_rows),
                "enabled_state_updates": sorted(applied_counts),
                "applied_feature_counts": applied_counts,
                "feature_universe_coverage": feature_universe_coverage,
                "ablation_results": ablation_count,
                "ablation_not_computed": total_signal_slots - ablation_count,
                "default_objective": objectives_config.default_objective,
                "objective_diagnostics": objective_diagnostics,
                "reliability_diagnostics": reliability_diagnostics,
                "path_risk_diagnostics": path_risk_diagnostics,
            }
        return {
            "run_id": str(run_id),
            "status": "succeeded",
            "population": len(items),
            "input_ready": len(ready),
            "base_distributions": base_count,
            "phase2_distributions": base_count,
            "phase3_distributions": base_count,
            "phase4_distributions": base_count,
            "phase5_distributions": base_count,
            "phase6_distributions": base_count,
            "empty_distributions": empty_count,
            "objective_scores": sum(len(rows) for rows in objective_rows.values()),
            "ablation_results": ablation_count,
        }
    except Exception as exc:
        with session_scope() as session:
            run = session.get(ModelRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.datetime.now(datetime.timezone.utc)
                run.warnings = [*(run.warnings or []), f"{type(exc).__name__}: {exc}"[:2000]]
        raise
