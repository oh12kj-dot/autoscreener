"""Append-only Model v5 shadow runner with Phase 2 distribution outputs."""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
import uuid
from dataclasses import replace

from autoscreener.config import (
    ModelV5Config,
    ObjectivesConfig,
    load_model_v5_config,
    load_objectives_config,
    load_scoring_config,
)
from autoscreener.dates import utc_today
from autoscreener.db.models import ModelRun, ModelScore, ObjectiveScore
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
from autoscreener.scoring.v5.feature_registry import feature_registry_payload
from autoscreener.scoring.v5.growth import (
    GrowthFeatureSet,
    GrowthUpdate,
    apply_growth_features,
    build_growth_feature_sets,
)
from autoscreener.scoring.v5.inputs import V5PitInput, build_v5_pit_inputs
from autoscreener.scoring.v5.objectives import evaluate_objectives
from autoscreener.scoring.v5.quality import (
    QualityFeatureSet,
    QualityUpdate,
    apply_quality_features,
    build_quality_feature_sets,
)
from autoscreener.scoring.v5.scenario import build_scenarios
from autoscreener.scoring.v5.state_model import build_future_state

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
    growth_update: GrowthUpdate | None,
    quality_update: QualityUpdate | None,
    capital_update: CapitalUpdate | None,
    ablation: dict,
) -> dict:
    payload = {
        "registry_version": "phase5",
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
        "ablation": ablation,
    }
    if growth_update is not None:
        payload["growth_update"] = growth_update.to_dict()
    if quality_update is not None:
        payload["quality_update"] = quality_update.to_dict()
    if capital_update is not None:
        payload["capital_update"] = capital_update.to_dict()
    return payload


def _score_warnings(
    item: V5PitInput, has_distribution: bool,
    growth_features: GrowthFeatureSet, quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet,
) -> list[str]:
    warnings = ["phase5_state_updates_shadow_only", "not_for_production"]
    if item.raw_snapshot_id is not None:
        warnings.append("financial_statement_pit_is_approximate")
    if item.input_status == "not_collected":
        warnings.append("raw_snapshot_not_available_as_of")
    elif not has_distribution:
        warnings.append("distribution_unavailable")
    disabled = [
        signal.key
        for signal in (*growth_features.signals, *quality_features.signals, *capital_features.signals)
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
    base_confidence: float,
    model_config: ModelV5Config,
) -> tuple[GrowthUpdate, QualityUpdate, CapitalUpdate, float, dict]:
    """Compute one (growth_update, quality_update, capital_update, confidence,
    distribution) tuple.

    The single place Phase 3, 4, and 5 state updates are combined into a
    distribution, so the leave-one-out ablation loop below can call this
    with any signal excluded from any feature set and get a directly
    comparable counterfactual, instead of duplicating the combination logic
    per phase (handoff 4.8: do not copy-paste the Phase 3 ablation loop).
    """
    confidence = max(
        0.0,
        min(
            1.0,
            base_confidence + growth_features.confidence_delta
            + quality_features.confidence_delta + capital_features.confidence_delta,
        ),
    )
    growth_update = apply_growth_features(result, growth_features, config=model_config)
    quality_update = apply_quality_features(
        result, quality_features, config=model_config, growth_update=growth_update,
    )
    capital_update = apply_capital_features(result, capital_features, config=model_config)
    mean_multiplier = growth_update.revenue_multiple_ratio * quality_update.mean_multiplier
    scenarios = build_scenarios(
        result, confidence=confidence, config=model_config,
        conditional_mean_multiplier=mean_multiplier,
        sigma_multiplier=quality_update.sigma_multiplier,
        left_tail_extra=quality_update.left_tail_extra,
        survival_multiplier=capital_update.survival_multiplier,
    )
    distribution = scenario_distribution(
        scenarios, horizon_years=model_config.target_horizon_years,
        target_moic=model_config.target_moic, confidence=confidence,
    )
    return growth_update, quality_update, capital_update, confidence, distribution


def _ablate(
    key: str,
    *,
    result: MoicResult,
    growth_features: GrowthFeatureSet,
    quality_features: QualityFeatureSet,
    capital_features: CapitalFeatureSet,
    base_confidence: float,
    model_config: ModelV5Config,
    full_growth_update: GrowthUpdate,
    full_quality_update: QualityUpdate,
    full_capital_update: CapitalUpdate,
    full_confidence: float,
    full_distribution: dict,
) -> dict:
    """Leave-one-feature-out counterfactual, growth/quality/capital key alike."""
    is_growth_key = any(signal.key == key for signal in growth_features.signals)
    is_quality_key = any(signal.key == key for signal in quality_features.signals)
    without_growth = growth_features.excluding(key) if is_growth_key else growth_features
    without_quality = quality_features.excluding(key) if is_quality_key else quality_features
    without_capital = (
        capital_features.excluding(key) if not is_growth_key and not is_quality_key
        else capital_features
    )
    (
        without_growth_update, without_quality_update, without_capital_update,
        without_confidence, without_distribution,
    ) = _distribution_for(
        result, growth_features=without_growth, quality_features=without_quality,
        capital_features=without_capital, base_confidence=base_confidence,
        model_config=model_config,
    )
    full_duration = full_growth_update.updated_duration_years * full_quality_update.duration_multiplier
    without_duration = without_growth_update.updated_duration_years * without_quality_update.duration_multiplier
    full_mean = full_growth_update.revenue_multiple_ratio * full_quality_update.mean_multiplier
    without_mean = without_growth_update.revenue_multiple_ratio * without_quality_update.mean_multiplier
    return {
        "status": "computed",
        "state_shift": {
            "initial_growth_rate": (
                full_growth_update.updated_initial_rate - without_growth_update.updated_initial_rate
            ),
            "growth_duration_years": full_duration - without_duration,
            "revenue_multiple_ratio": full_mean - without_mean,
            "sigma_multiplier": full_quality_update.sigma_multiplier - without_quality_update.sigma_multiplier,
            "left_tail_extra": full_quality_update.left_tail_extra - without_quality_update.left_tail_extra,
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
            warnings=["phase5_capital_state_updates", "v4_champion_unchanged"],
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
            base_count = 0
            empty_count = 0
            objective_rows: dict[str, list[ObjectiveScore]] = {}
            applied_counts: dict[str, int] = {}
            ablation_count = 0
            total_signal_slots = 0
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
                all_signals = (
                    *growth_features.signals, *quality_features.signals, *capital_features.signals,
                )
                total_signal_slots += len(all_signals)
                base_confidence = (
                    model_config.reliability.ready_input_confidence
                    if has_distribution else model_config.reliability.unavailable_input_confidence
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
                if result is None:
                    confidence = base_confidence
                    distribution = unavailable_distribution(
                        target_moic=model_config.target_moic, confidence=confidence
                    )
                else:
                    (
                        growth_update, quality_update, capital_update, confidence, distribution,
                    ) = _distribution_for(
                        result, growth_features=growth_features, quality_features=quality_features,
                        capital_features=capital_features, base_confidence=base_confidence,
                        model_config=model_config,
                    )
                    if quality_update.no_effect_keys:
                        quality_features = _reconcile_quality_features(quality_features, quality_update)
                        for key in quality_update.no_effect_keys:
                            ablation[key] = {
                                "status": "not_computed",
                                "reason": "no_change_zero_growth_or_reduction",
                            }
                    applied_keys = (
                        *growth_update.applied_keys, *quality_update.applied_keys,
                        *capital_update.applied_keys,
                    )
                    ablation_enabled = (
                        model_config.growth.ablation_enabled
                        and model_config.quality.ablation_enabled
                        and model_config.capital.ablation_enabled
                    )
                    for key in applied_keys:
                        applied_counts[key] = applied_counts.get(key, 0) + 1
                        if not ablation_enabled:
                            continue
                        ablation[key] = _ablate(
                            key, result=result, growth_features=growth_features,
                            quality_features=quality_features, capital_features=capital_features,
                            base_confidence=base_confidence, model_config=model_config,
                            full_growth_update=growth_update, full_quality_update=quality_update,
                            full_capital_update=capital_update, full_confidence=confidence,
                            full_distribution=distribution,
                        )
                        ablation_count += 1
                future_state = build_future_state(
                    result,
                    item.moic_inputs,
                    horizon_years=model_config.target_horizon_years,
                    confidence=confidence,
                    growth_update=growth_update,
                    quality_update=quality_update,
                    capital_update=capital_update,
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
                        growth_update, quality_update, capital_update, ablation,
                    ),
                    confidence=confidence,
                    warnings=_score_warnings(
                        item, has_distribution, growth_features, quality_features, capital_features
                    ),
                ))
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
                "empty_distributions": empty_count,
                "objective_scores": sum(len(rows) for rows in objective_rows.values()),
                "enabled_objectives": sorted(objective_rows),
                "enabled_state_updates": sorted(applied_counts),
                "applied_feature_counts": applied_counts,
                "feature_universe_coverage": feature_universe_coverage,
                "ablation_results": ablation_count,
                "ablation_not_computed": total_signal_slots - ablation_count,
                "default_objective": objectives_config.default_objective,
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
