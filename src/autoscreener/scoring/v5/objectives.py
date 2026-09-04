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
        elif name == "risk_adjusted_compounding":
            # WP-B (docs/racr_wp_b_output_contract_2026-09-04.md; audit
            # autoscreener_racr_integrated_redesign_audit_2026-09-04.md
            # §4.3/§5.2): RACR replaces `risk_adjusted`'s "expected CAGR
            # minus loss *depth*, ignoring loss *probability*" formula
            # (§4.3's diagnosed defect) with a certainty-equivalent CAGR
            # that already prices in failure-atom probability via
            # `E[ln W]` (see `distribution.py`'s `_expected_log_moic`),
            # plus explicit tail/drawdown/permanent-loss/uncertainty
            # penalty terms.
            #
            # `ce_cagr` and `expected_shortfall_10pct_log` are read
            # directly from the distribution (single source of truth --
            # this objective never recomputes the CDF itself, matching how
            # `risk_adjusted` already reads `expected_moic_given_loss`
            # rather than re-deriving it).
            ce_cagr = distribution["ce_cagr"]
            # TailLoss10 (audit §5.2): "the average annualized log-loss in
            # the worst decile of outcomes", floored at 0 so a ticker whose
            # worst decile is still a gain contributes no penalty.
            tail_loss_10 = max(0.0, -distribution["expected_shortfall_10pct_log"])
            tail_lambda = definition.tail_lambda or 0.0
            drawdown_lambda = definition.drawdown_lambda or 0.0
            permanent_loss_lambda = definition.permanent_loss_lambda or 0.0
            uncertainty_lambda = definition.uncertainty_lambda or 0.0
            # DDExcess and P(PermanentLoss) are `unavailable` in the
            # distribution contract (no path simulation / competing-risk
            # model yet -- see distribution.py's WP-B additions). They
            # are computed as exactly 0 here, which is *not* the same
            # claim as "distribution.py reports 0 risk": the distribution
            # fields themselves stay None+reason. `omitted_terms` below
            # exists specifically so this score is never misread as
            # "already risk-adjusted for drawdown/permanent loss" -- per
            # the plan's explicit requirement that this must not be papered
            # over.
            dd_excess = 0.0
            p_permanent_loss = 0.0
            model_confidence = distribution.get("model_confidence") or 0.0
            # ModelUncertainty (audit §5.2: "CE CAGR推定標準誤差を半分控除",
            # lambda_U initial value 0.50): a proxy for the standard error
            # of the CE CAGR point estimate, derived from `model_confidence`
            # per the plan's explicit instruction -- not from the
            # distribution's own sigma, which already feeds `ce_cagr` and
            # `tail_loss_10` and would double-count if reused here.
            # Interpolates linearly between "fully confident: no penalty"
            # (model_confidence=1.0) and "zero confidence: treat the whole
            # magnitude of the CE CAGR estimate as one standard error of
            # uncertainty" (model_confidence=0.0).
            model_uncertainty = (1.0 - model_confidence) * abs(ce_cagr)
            value = (
                ce_cagr
                - tail_lambda * tail_loss_10
                - drawdown_lambda * dd_excess
                - permanent_loss_lambda * p_permanent_loss
                - uncertainty_lambda * model_uncertainty
            )
            explanation = {
                "formula": (
                    "ce_cagr - tail_lambda*TailLoss10 - drawdown_lambda*DDExcess "
                    "- permanent_loss_lambda*P(PermanentLoss) - uncertainty_lambda*ModelUncertainty"
                ),
                "ce_cagr": ce_cagr,
                "ce_cagr_failure_floor": distribution.get("ce_cagr_failure_floor"),
                "tail_loss_10": tail_loss_10,
                "dd_excess": dd_excess,
                "p_permanent_loss": p_permanent_loss,
                "model_uncertainty": model_uncertainty,
                "model_confidence": model_confidence,
                "tail_lambda": tail_lambda,
                "drawdown_lambda": drawdown_lambda,
                "permanent_loss_lambda": permanent_loss_lambda,
                "uncertainty_lambda": uncertainty_lambda,
                # Required by the plan (B-3): must always be present while
                # drawdown/permanent-loss remain unavailable, so nothing
                # downstream can read this score as risk-complete.
                "omitted_terms": ["drawdown", "permanent_loss"],
            }
        else:
            results[name] = ObjectiveResult(name, "unsupported", None, {"reason": "objective_requires_later_phase_inputs"})
            continue
        results[name] = ObjectiveResult(name, "available", float(value), explanation)
    return results
