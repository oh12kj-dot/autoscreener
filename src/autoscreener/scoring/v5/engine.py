"""Append-only Model v5 shadow runner with Phase 2 distribution outputs."""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid

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

_IMPLEMENTATION_VERSION = "v5.phase4"


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
    growth_update: GrowthUpdate | None,
    quality_update: QualityUpdate | None,
    ablation: dict,
) -> dict:
    payload = {
        "registry_version": "phase4",
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
        "ablation": ablation,
    }
    if growth_update is not None:
        payload["growth_update"] = growth_update.to_dict()
    if quality_update is not None:
        payload["quality_update"] = quality_update.to_dict()
    return payload


def _score_warnings(
    item: V5PitInput, has_distribution: bool,
    growth_features: GrowthFeatureSet, quality_features: QualityFeatureSet,
) -> list[str]:
    warnings = ["phase4_state_updates_shadow_only", "not_for_production"]
    if item.raw_snapshot_id is not None:
        warnings.append("financial_statement_pit_is_approximate")
    if item.input_status == "not_collected":
        warnings.append("raw_snapshot_not_available_as_of")
    elif not has_distribution:
        warnings.append("distribution_unavailable")
    disabled = [
        signal.key for signal in (*growth_features.signals, *quality_features.signals)
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
    base_confidence: float,
    model_config: ModelV5Config,
) -> tuple[GrowthUpdate, QualityUpdate, float, dict]:
    """Compute one (growth_update, quality_update, confidence, distribution) tuple.

    The single place both Phase 3 and Phase 4 state updates are combined into
    a distribution, so the leave-one-out ablation loop below can call this
    with any signal excluded from either feature set and get a directly
    comparable counterfactual, instead of duplicating the combination logic
    per phase (handoff 4.8: do not copy-paste the Phase 3 ablation loop).
    """
    confidence = max(
        0.0,
        min(1.0, base_confidence + growth_features.confidence_delta + quality_features.confidence_delta),
    )
    growth_update = apply_growth_features(result, growth_features, config=model_config)
    quality_update = apply_quality_features(result, quality_features, config=model_config)
    mean_multiplier = growth_update.revenue_multiple_ratio * quality_update.mean_multiplier
    scenarios = build_scenarios(
        result, confidence=confidence, config=model_config,
        conditional_mean_multiplier=mean_multiplier,
        sigma_multiplier=quality_update.sigma_multiplier,
        left_tail_extra=quality_update.left_tail_extra,
    )
    distribution = scenario_distribution(
        scenarios, horizon_years=model_config.target_horizon_years,
        target_moic=model_config.target_moic, confidence=confidence,
    )
    return growth_update, quality_update, confidence, distribution


def _ablate(
    key: str,
    *,
    result: MoicResult,
    growth_features: GrowthFeatureSet,
    quality_features: QualityFeatureSet,
    base_confidence: float,
    model_config: ModelV5Config,
    full_growth_update: GrowthUpdate,
    full_quality_update: QualityUpdate,
    full_confidence: float,
    full_distribution: dict,
) -> dict:
    """Leave-one-feature-out counterfactual, growth or quality key alike."""
    is_growth_key = any(signal.key == key for signal in growth_features.signals)
    without_growth = growth_features.excluding(key) if is_growth_key else growth_features
    without_quality = quality_features if is_growth_key else quality_features.excluding(key)
    without_growth_update, without_quality_update, without_confidence, without_distribution = _distribution_for(
        result, growth_features=without_growth, quality_features=without_quality,
        base_confidence=base_confidence, model_config=model_config,
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
            "model_confidence": full_confidence - without_confidence,
        },
        "scenario_impact": {
            "p_target": full_distribution["p_target"] - without_distribution["p_target"],
            "expected_cagr": full_distribution["expected_cagr"] - without_distribution["expected_cagr"],
        },
        "without_feature": {
            "p_target": without_distribution["p_target"],
            "expected_cagr": without_distribution["expected_cagr"],
            "model_confidence": without_confidence,
        },
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
            metrics=None,
            warnings=["phase4_quality_state_updates", "v4_champion_unchanged"],
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
                all_signals = (*growth_features.signals, *quality_features.signals)
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
                if result is None:
                    confidence = base_confidence
                    distribution = unavailable_distribution(
                        target_moic=model_config.target_moic, confidence=confidence
                    )
                else:
                    growth_update, quality_update, confidence, distribution = _distribution_for(
                        result, growth_features=growth_features, quality_features=quality_features,
                        base_confidence=base_confidence, model_config=model_config,
                    )
                    applied_keys = (*growth_update.applied_keys, *quality_update.applied_keys)
                    ablation_enabled = (
                        model_config.growth.ablation_enabled and model_config.quality.ablation_enabled
                    )
                    for key in applied_keys:
                        applied_counts[key] = applied_counts.get(key, 0) + 1
                        if not ablation_enabled:
                            continue
                        ablation[key] = _ablate(
                            key, result=result, growth_features=growth_features,
                            quality_features=quality_features, base_confidence=base_confidence,
                            model_config=model_config, full_growth_update=growth_update,
                            full_quality_update=quality_update, full_confidence=confidence,
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
                    contract_version=_IMPLEMENTATION_VERSION,
                )
                session.add(ModelScore(
                    run_id=run_id,
                    ticker_id=item.ticker_id,
                    target_horizon_years=model_config.target_horizon_years,
                    target_moic=model_config.target_moic,
                    distribution=distribution,
                    states=future_state.to_dict(),
                    features=_feature_payload(
                        item, registry, growth_features, quality_features,
                        growth_update, quality_update, ablation,
                    ),
                    confidence=confidence,
                    warnings=_score_warnings(item, has_distribution, growth_features, quality_features),
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
            run.metrics = {
                "input_ready": len(ready),
                "base_distributions": base_count,
                "phase2_distributions": base_count,
                "phase3_distributions": base_count,
                "phase4_distributions": base_count,
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
