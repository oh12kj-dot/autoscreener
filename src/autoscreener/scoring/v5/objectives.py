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
            raw_p_target = distribution["p_target"]
            sigma_multiplier = distribution.get("reliability_sigma_multiplier")
            left_tail_extra = distribution.get("reliability_left_tail_extra")
            sigma_lambda = definition.reliability_sigma_lambda or 0.0
            left_tail_lambda = definition.reliability_left_tail_lambda or 0.0
            # Phase 10 fix (docs/model_v5_phase10_*.md): a mean-preserving
            # widening of the return distribution mechanically raises
            # P(MOIC >= target_moic) for a far right-tail target like 10x --
            # a structural property of the lognormal-mixture family, true
            # regardless of *why* the widening happened. Measured real
            # example: accounting_quality severity (which only ever widens
            # sigma/left-tail, per Issue section 6.3 -- it must never lower
            # the modeled mean) raised P(target) in 712/712 real ablations.
            # Discounting the *ranking statistic* here (never the
            # distribution's own mean, sigma, or p_target field -- those are
            # untouched) stops this objective from rewarding an increase in
            # modeled uncertainty, without re-litigating section 6.3's ban
            # on turning quality concerns into a mean-reducing penalty.
            reliability_widening = 0.0
            if sigma_multiplier is not None:
                reliability_widening += sigma_lambda * max(0.0, sigma_multiplier - 1.0)
            if left_tail_extra is not None:
                reliability_widening += left_tail_lambda * left_tail_extra
            value = raw_p_target / (1.0 + reliability_widening)
            explanation = {
                "formula": "P(MOIC >= target_moic) / (1 + sigma_lambda*max(0,sigma_multiplier-1) + left_tail_lambda*left_tail_extra)",
                "target_moic": distribution["target_moic"],
                "raw_p_target": raw_p_target,
                "reliability_widening": reliability_widening,
                "reliability_sigma_multiplier": sigma_multiplier,
                "reliability_left_tail_extra": left_tail_extra,
            }
        elif name == "expected_return":
            value = distribution["expected_cagr"]
            explanation = {"formula": "expected_moic ** (1/horizon_years) - 1"}
        elif name == "risk_adjusted":
            # Phase 10 fix (docs/model_v5_phase10_*.md): `expected_shortfall_10pct`
            # is defined at a *fixed probability level* (its own 10% quantile).
            # Measured: whenever the failure atom alone (1 - survival) is
            # already >= 10% -- true for 100% of the real 2026-09-02
            # universe -- that quantile falls exactly on MOIC=0 and the
            # measure is *identically* 0.0 for every ticker, so `risk_adjusted`
            # was silently reproducing `expected_return`'s exact rank order
            # (Spearman 1.0). `expected_moic_given_loss` (a *fixed-cutoff*
            # E[MOIC | MOIC < 1.0], added to the distribution contract this
            # phase) does not have this failure mode: see
            # `_conditional_expected_moic_below`'s docstring. The old
            # `expected_shortfall_10pct` field/formula stays in the
            # distribution contract unchanged for backward compatibility
            # (still a mathematically valid statistic; anything reading it
            # directly keeps working) -- only this objective's formula
            # switches inputs.
            downside_lambda = definition.downside_lambda or 0.0
            loss_moic = distribution.get("expected_moic_given_loss")
            if loss_moic is None:
                # Essentially nobody loses money at the MOIC<1.0 cutoff for
                # this ticker -- an honest "no downside to price in", not a
                # fabricated 0% or -100% loss.
                downside_risk = 0.0
                loss_cagr = None
            else:
                loss_cagr = _annualized(loss_moic, horizon_years)
                downside_risk = max(0.0, -loss_cagr)
            value = distribution["expected_cagr"] - downside_lambda * downside_risk
            explanation = {
                "formula": "expected_cagr - lambda * max(0, -CAGR(E[MOIC | MOIC < 1.0]))",
                "lambda": downside_lambda,
                "expected_moic_given_loss": loss_moic,
                "expected_moic_given_loss_cagr": loss_cagr,
            }
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
