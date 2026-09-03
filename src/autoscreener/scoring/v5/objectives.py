"""Ranking objectives computed solely from one immutable distribution."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from autoscreener.config import ObjectivesConfig


@dataclass(frozen=True)
class ObjectiveResult:
    objective: str
    status: str
    score_value: float | None
    explanation: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _annualized(moic: float, horizon_years: int) -> float:
    return moic ** (1.0 / horizon_years) - 1.0 if moic > 0 else -1.0


def evaluate_objectives(distribution: dict, config: ObjectivesConfig, *, horizon_years: int) -> dict[str, ObjectiveResult]:
    """Evaluate every enabled objective; unsupported inputs stay explicit."""
    results: dict[str, ObjectiveResult] = {}
    available = distribution.get("status") == "available"
    for name, definition in config.objectives.items():
        if not definition.enabled:
            continue
        if not available:
            results[name] = ObjectiveResult(name, "unavailable", None, {"reason": "distribution_unavailable"})
            continue
        if name == "ten_bagger":
            value = distribution["p_target"]
            explanation = {"formula": "P(MOIC >= target_moic)", "target_moic": distribution["target_moic"]}
        elif name == "expected_return":
            value = distribution["expected_cagr"]
            explanation = {"formula": "expected_moic ** (1/horizon_years) - 1"}
        elif name == "risk_adjusted":
            downside_lambda = definition.downside_lambda or 0.0
            es_cagr = _annualized(distribution["expected_shortfall_10pct"], horizon_years)
            downside_risk = max(0.0, -es_cagr)
            value = distribution["expected_cagr"] - downside_lambda * downside_risk
            explanation = {"formula": "expected_cagr - lambda * max(0, -ES10_CAGR)",
                           "lambda": downside_lambda, "expected_shortfall_cagr": es_cagr}
        elif name == "asymmetric":
            threshold = definition.right_tail_moic or 5.0
            key = f"p_moic_{int(threshold)}x"
            right_tail = distribution.get(key)
            if right_tail is None:
                results[name] = ObjectiveResult(name, "unsupported", None, {"reason": f"missing_{key}"})
                continue
            left_tail = distribution["p_moic_below_0_5"]
            value = right_tail / max(left_tail, 1e-6)
            explanation = {"formula": f"P(MOIC >= {threshold:g}) / max(P(MOIC < 0.5), 1e-6)"}
        elif name == "capital_preservation":
            value = 1.0 - distribution["p_moic_below_1_0"]
            explanation = {"formula": "1 - P(MOIC < 1.0)"}
        else:
            results[name] = ObjectiveResult(name, "unsupported", None, {"reason": "objective_requires_later_phase_inputs"})
            continue
        results[name] = ObjectiveResult(name, "available", float(value), explanation)
    return results
