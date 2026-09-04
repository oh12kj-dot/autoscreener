"""Mathematically explicit Model v5 scenario-mixture distribution."""

from __future__ import annotations

import math
from statistics import NormalDist

from autoscreener.scoring.moic import MoicResult
from autoscreener.scoring.v5.path_risk import PathRiskResult
from autoscreener.scoring.v5.scenario import ReturnScenario

_NORMAL = NormalDist()

# WP-B (docs/racr_wp_b_output_contract_2026-09-04.md / audit
# autoscreener_racr_integrated_redesign_audit_2026-09-04.md §4.3, §5.2): the
# failure atom (bankruptcy/non-recovering delisting) is modelled as MOIC=0
# exactly. ``E[ln W]`` -- the input to certainty-equivalent CAGR -- is
# therefore ``(1-survival) * ln(0) + ...`` which is -infinity for *any*
# nonzero failure probability, not just an edge case: essentially every
# ticker in the real universe has survival < 1. We do not have a recovery-
# rate distribution yet (that requires the delisting cause/settlement
# backfill this audit calls out as unimplemented), so instead of silently
# dropping the failure atom from the expectation -- which would overstate
# every ticker's CE CAGR by treating bankruptcy as a non-event -- we floor
# the failure atom's MOIC at this value before taking logs. The floor is
# recorded on every available distribution as ``ce_cagr_failure_floor`` so
# nothing downstream can mistake "computed" for "unbiased": 0.01x means the
# floor still asserts a 99% loss is realized on failure, it just avoids the
# -inf singularity so CE CAGR remains a finite, rankable number.
CE_CAGR_FAILURE_FLOOR_MOIC = 0.01


def base_distribution(result: MoicResult | None, *, target_moic: float) -> dict:
    """Backward-compatible Phase 1 seed serializer used by old persisted runs."""
    if result is None:
        return {"contract_version": "v5.phase1.base", "status": "unavailable",
                "distribution_family": None, "source_model_version": "v4", "p_target": None,
                "target_moic": target_moic, "expected_moic": None, "median_moic": None,
                "log_moic_mu": None, "log_moic_sigma": None, "survival_probability": None}
    return {"contract_version": "v5.phase1.base", "status": "base_only",
            "distribution_family": "v4_lognormal_seed", "source_model_version": "v4",
            "p_target": result.probability, "target_moic": target_moic,
            "expected_moic": result.expected_moic, "median_moic": result.median_moic,
            "log_moic_mu": result.log_moic_mu, "log_moic_sigma": result.log_moic_sigma,
            "survival_probability": result.survival_probability}


def _cdf(value: float, scenarios: tuple[ReturnScenario, ...], survival: float) -> float:
    if value <= 0:
        return 1.0 - survival
    conditional = sum(
        scenario.weight * _NORMAL.cdf(
            (math.log(value) - scenario.log_mu) / scenario.log_sigma
        )
        for scenario in scenarios
    )
    return min(1.0, max(0.0, (1.0 - survival) + survival * conditional))


def _quantile(q: float, scenarios: tuple[ReturnScenario, ...], survival: float) -> float:
    failure_mass = 1.0 - survival
    if q <= failure_mass or survival <= 0:
        return 0.0
    low = min(s.log_mu - 12.0 * s.log_sigma for s in scenarios)
    high = max(s.log_mu + 12.0 * s.log_sigma for s in scenarios)
    for _ in range(100):
        mid = (low + high) / 2.0
        if _cdf(math.exp(mid), scenarios, survival) < q:
            low = mid
        else:
            high = mid
    return math.exp((low + high) / 2.0)


def _expected_shortfall(
    alpha: float, scenarios: tuple[ReturnScenario, ...], survival: float
) -> float:
    if 1.0 - survival >= alpha:
        return 0.0
    cutoff = _quantile(alpha, scenarios, survival)
    truncated = 0.0
    for scenario in scenarios:
        z = (
            math.log(cutoff) - scenario.log_mu - scenario.log_sigma**2
        ) / scenario.log_sigma
        truncated += (
            scenario.weight
            * scenario.conditional_expected_moic
            * _NORMAL.cdf(z)
        )
    return survival * truncated / alpha


def _conditional_expected_moic_below(
    cutoff: float, scenarios: tuple[ReturnScenario, ...], survival: float
) -> float | None:
    """``E[MOIC | MOIC < cutoff]`` -- Phase 10 fix (docs/model_v5_phase10_*.md).

    ``expected_shortfall_10pct`` (``_expected_shortfall`` above) is defined
    at a *fixed probability level* (its own 10% quantile). When the failure
    atom alone already exceeds that level -- true for essentially every
    ticker in the real universe, since ``1.0 - survival`` commonly exceeds
    0.10 -- the 10%-quantile falls exactly on MOIC=0 and the whole measure
    collapses to a constant 0.0 for every ticker (measured: 100% of 1,164
    available rows on 2026-09-02). A constant carries no ranking
    information, which is why ``risk_adjusted`` was silently reproducing
    ``expected_return``'s exact rank order.

    This is a *fixed-cutoff* conditional expectation instead: "if this
    investment loses money (MOIC < 1.0), how much is lost on average,
    across both the failure atom (contributes exactly 0 to the numerator,
    same math as `_expected_shortfall`) and the surviving-but-low-return
    part of the continuous mixture below 1.0x". Because the *ratio* of
    failure-atom mass to sub-1.0x continuous mass differs by ticker
    (varying survival, sigma, conditional means), this does not collapse
    to a constant the way the alpha-quantile measure does -- see the
    Phase 10 evidence doc for the measured cross-sectional spread.

    Returns ``None`` (not 0.0) when essentially nobody loses money at this
    cutoff (``P(MOIC < cutoff)`` below a numerical floor) -- an honest
    "undefined", not a fabricated worst-case or best-case number.
    """
    mass_below = _cdf(cutoff, scenarios, survival)
    if mass_below < 1e-9:
        return None
    truncated = 0.0
    for scenario in scenarios:
        z = (
            math.log(cutoff) - scenario.log_mu - scenario.log_sigma**2
        ) / scenario.log_sigma
        truncated += (
            scenario.weight
            * scenario.conditional_expected_moic
            * _NORMAL.cdf(z)
        )
    return survival * truncated / mass_below


def _expected_log_moic(
    scenarios: tuple[ReturnScenario, ...], survival: float, floor_moic: float
) -> float:
    """``E[ln W_H]`` with the failure atom floored at ``floor_moic``.

    Each scenario's continuous part is lognormal(log_mu, log_sigma), for
    which ``E[ln X] == log_mu`` exactly (no sigma correction -- that
    correction only applies to ``E[X]`` itself, e.g. ``conditional_expected_moic``
    in scenario.py). The failure atom contributes ``ln(floor_moic)`` weighted
    by its own probability mass, per the floor rationale on
    ``CE_CAGR_FAILURE_FLOOR_MOIC`` above.
    """
    continuous = sum(scenario.weight * scenario.log_mu for scenario in scenarios)
    return (1.0 - survival) * math.log(floor_moic) + survival * continuous


def _expected_log_moic_below_quantile_given_survival(
    q: float, scenarios: tuple[ReturnScenario, ...]
) -> float:
    """``E[ln W | survive, W <= Quantile_q(W | survive)]`` -- WP-B2 fix
    (docs/racr_wp_b2_risk_terms_2026-09-04.md; diagnostic
    docs/racr_shadow_run_diagnostic_2026-09-04.md §3.1).

    The WP-B ``expected_shortfall_10pct_log`` measures the worst *decile of
    the unconditional distribution*, which includes the failure atom. Every
    real ticker's failure mass (``1 - survival``) turned out to exceed 10%
    (measured: 1,157/1,157 on the 2026-09-04 shadow run), so that 10%
    quantile always landed *inside* the failure atom and the conditional
    expectation collapsed to the identical constant ``ln(floor_moic)/H`` for
    every ticker -- a constant carries no ranking information, reproducing
    exactly the defect Phase 10 already fixed once for the MOIC-space
    measure (see ``_conditional_expected_moic_below``'s docstring).

    This measures the tail conditional on *surviving* instead: exclude the
    failure atom entirely, and take the worst decile of the continuous
    lognormal-mixture part on its own. Conditioning on survival is
    mathematically simple here because the mixture already sums to
    probability 1 -- ``P(W in dw | survive) = sum_i weight_i *
    lognormal_i(w) dw``, i.e. exactly the same mixture with no atom and no
    survival-scaling factor. That is exactly what calling
    ``_expected_log_moic_below_quantile`` with ``survival=1.0`` computes:
    the ``failure_mass >= q`` branch never triggers (failure_mass is 0),
    so the floor is never invoked either -- there is no failure atom left
    to floor once we have already conditioned it away. ``floor_moic`` is
    therefore irrelevant here and a dummy value is passed.
    """
    return _expected_log_moic_below_quantile(q, scenarios, 1.0, floor_moic=1.0)


def _expected_log_moic_below_quantile(
    q: float,
    scenarios: tuple[ReturnScenario, ...],
    survival: float,
    floor_moic: float,
) -> float:
    """``E[ln W_H | W_H <= Quantile_q(W_H)]``, floor applied the same way.

    If the failure atom alone already carries at least ``q`` probability
    mass, the entire bottom-``q`` slice sits inside the atom (``_quantile``
    already returns exactly 0.0 in that case) and the conditional
    expectation is just ``ln(floor_moic)``. Otherwise the bottom-``q`` slice
    spans the whole failure atom plus a truncated slice of the continuous
    mixture between 0 and the quantile cutoff; that slice's contribution to
    ``E[ln X * 1{X<=cutoff}]`` for a lognormal(mu, sigma) is the standard
    closed form ``mu*Phi(z) - sigma*phi(z)`` with
    ``z=(ln(cutoff)-mu)/sigma`` (derived from ``Y=mu+sigma*Z``,
    ``E[Y*1{Z<=z}] = mu*Phi(z) + sigma*E[Z*1{Z<=z}] = mu*Phi(z) - sigma*phi(z)``).
    Dividing the total by ``q`` turns the unconditional contribution into
    the conditional expectation, since by construction ``P(W<=cutoff) == q``.
    """
    failure_mass = 1.0 - survival
    if failure_mass >= q:
        return math.log(floor_moic)
    cutoff = _quantile(q, scenarios, survival)
    total = failure_mass * math.log(floor_moic)
    for scenario in scenarios:
        z = (math.log(cutoff) - scenario.log_mu) / scenario.log_sigma
        total += survival * scenario.weight * (
            scenario.log_mu * _NORMAL.cdf(z) - scenario.log_sigma * _NORMAL.pdf(z)
        )
    return total / q


def unavailable_distribution(*, target_moic: float, confidence: float) -> dict:
    fields = {
        "p_moic_below_0_5": None, "p_moic_below_1_0": None,
        "p_moic_2x": None, "p_moic_3x": None, "p_moic_5x": None,
        "p_moic_10x": None, "p_target": None, "expected_moic": None,
        "median_moic": None, "expected_cagr": None, "median_cagr": None,
        "expected_shortfall_10pct": None, "p10_moic": None,
        "p25_moic": None, "p50_moic": None, "p75_moic": None,
        "p90_moic": None, "survival_probability": None,
        "acquisition_probability": None,
        # Phase 10 additions (docs/model_v5_phase10_*.md): additive-only,
        # never populated for an unavailable distribution, same as every
        # other field above.
        "expected_moic_given_loss": None,
        "reliability_sigma_multiplier": None,
        "reliability_left_tail_extra": None,
        # WP-B additions (docs/racr_wp_b_output_contract_2026-09-04.md):
        # additive-only, same as every field above -- an unavailable
        # distribution reports every derived quantity as None, never 0.
        "ce_cagr": None, "ce_cagr_failure_floor": None,
        "p_cagr_above_15": None, "p_cagr_above_20": None, "p_cagr_above_25": None,
        "expected_shortfall_10pct_log": None,
        # WP-B2 addition (docs/racr_wp_b2_risk_terms_2026-09-04.md):
        # additive-only, same as every field above.
        "expected_shortfall_10pct_log_given_survival": None,
        "p_terminal_wealth_below_0_5": None,
        # Permanent loss / drawdown: unimplemented regardless of whether
        # this ticker's base distribution is available, so the reason
        # strings stay None here too -- "distribution_unavailable" (set at
        # the top level via ``status``) is already the operative reason;
        # duplicating the not-implemented reason under a different status
        # would obscure which condition actually applies.
        "p_permanent_loss": None, "p_permanent_loss_unavailable_reason": None,
        # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): path-risk fields
        # are now implemented (see ``path_risk.py``/``scenario_distribution``
        # below), but an *unavailable* base distribution has no MoicResult
        # to build price-independent context from at all -- consistent with
        # every other field in this function, an unavailable distribution
        # reports these as None too, reason left None (the top-level
        # ``status: "unavailable"`` is already the operative reason, same
        # convention as ``p_permanent_loss`` above).
        "expected_max_drawdown": None, "expected_max_drawdown_unavailable_reason": None,
        "p_mdd_above_30": None, "p_mdd_above_30_unavailable_reason": None,
        "p_mdd_above_50": None, "p_mdd_above_50_unavailable_reason": None,
        "p_mdd_above_70": None, "p_mdd_above_70_unavailable_reason": None,
        "expected_drawdown_excess_35": None, "expected_drawdown_excess_35_unavailable_reason": None,
        "recovery_time_median": None, "recovery_time_median_unavailable_reason": None,
        "recovery_time_p90": None, "recovery_time_p90_unavailable_reason": None,
        "path_risk_method": None, "path_risk_horizon_years": None,
        "path_risk_observations_used": None, "path_risk_simulations": None,
    }
    return {
        "contract_version": "v5.racr3", "status": "unavailable",
        "distribution_family": None,
        "source_model_version": "v4_structural_seed",
        "target_moic": target_moic, "model_confidence": confidence,
        "scenarios": [], **fields,
    }


_PATH_RISK_NOT_PROVIDED_REASON = "path_simulation_not_provided"


def scenario_distribution(
    scenarios: tuple[ReturnScenario, ...],
    *,
    horizon_years: int,
    target_moic: float,
    confidence: float,
    sigma_multiplier: float = 1.0,
    left_tail_extra: float = 0.0,
    path_risk: PathRiskResult | None = None,
) -> dict:
    """Return the full Phase 2 contract, including failure mass and ES10.

    ``sigma_multiplier``/``left_tail_extra`` (Phase 10 addition) are the
    *same* values already passed into ``build_scenarios`` by the caller --
    they do not change anything about how ``scenarios`` was built (this
    function never re-derives or second-guesses the scenario mixture it is
    handed). They are recorded here purely as diagnostic passthrough fields
    (``reliability_sigma_multiplier``/``reliability_left_tail_extra``) so
    ``objectives.py`` can read, from the persisted distribution alone, how
    much of this ticker's dispersion was widened by a reliability/quality
    signal -- needed to stop ``ten_bagger`` from mechanically rewarding
    that widening (see docs/model_v5_phase10_*.md). Purely additive: no
    existing field's value changes because of these two parameters.

    ``path_risk`` (WP-F1, docs/racr_wp_f1_path_risk_2026-09-04.md) is a
    ``path_risk.PathRiskResult`` computed *outside* this function, from a
    ticker's own realized ``price_snapshots`` history -- never derived from
    ``scenarios``/``survival`` (see path_risk.py's module docstring for why
    that independence is the entire point of this work package).
    ``None`` (the default) means the caller did not attempt a price-history
    estimate at all, reported as ``path_simulation_not_provided`` --
    distinct from a real attempt that came back ``unavailable`` (e.g.
    ``insufficient_price_history``), which is reported using that
    ``PathRiskResult``'s own reason.
    """
    if not scenarios:
        return unavailable_distribution(
            target_moic=target_moic, confidence=confidence
        )
    survival = scenarios[0].survival_probability
    if any(abs(s.survival_probability - survival) > 1e-12 for s in scenarios):
        raise ValueError("Phase 2 scenarios must share a survival probability")
    if abs(sum(s.weight for s in scenarios) - 1.0) > 1e-9:
        raise ValueError("scenario weights must sum to one")

    def exceed(threshold: float) -> float:
        return max(0.0, 1.0 - _cdf(threshold, scenarios, survival))

    expected_moic = survival * sum(
        s.weight * s.conditional_expected_moic for s in scenarios
    )
    quantiles = {
        q: _quantile(q, scenarios, survival)
        for q in (0.10, 0.25, 0.50, 0.75, 0.90)
    }
    median = quantiles[0.50]
    expected_cagr = expected_moic ** (1.0 / horizon_years) - 1.0
    median_cagr = median ** (1.0 / horizon_years) - 1.0 if median > 0 else -1.0
    p_moic_below_0_5 = _cdf(0.5, scenarios, survival)

    # WP-B (docs/racr_wp_b_output_contract_2026-09-04.md, plan §3 B-2/B-3;
    # audit §5.2/§6.1): certainty-equivalent CAGR and the log-CAGR
    # expected shortfall, both derived from the *same* CDF/scenario mixture
    # as every field above -- no separate model, no separate calibration.
    # See ``_expected_log_moic``/``_expected_log_moic_below_quantile`` for
    # why the failure atom must be floored rather than dropped.
    expected_log_moic = _expected_log_moic(scenarios, survival, CE_CAGR_FAILURE_FLOOR_MOIC)
    ce_cagr = math.exp(expected_log_moic / horizon_years) - 1.0
    expected_log_moic_below_p10 = _expected_log_moic_below_quantile(
        0.10, scenarios, survival, CE_CAGR_FAILURE_FLOOR_MOIC
    )
    expected_shortfall_10pct_log = expected_log_moic_below_p10 / horizon_years

    # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md; diagnostic
    # docs/racr_shadow_run_diagnostic_2026-09-04.md §3.1): the measure above
    # (``expected_shortfall_10pct_log``) is kept byte-for-byte unchanged for
    # backward compatibility, but it is the exact defect this WP fixes --
    # every real ticker's failure mass exceeds 10%, so that field is the
    # identical constant ``ln(floor)/H`` for the whole universe and carries
    # no ranking information. This new field measures the worst decile
    # *conditional on survival* -- the failure atom excluded entirely, not
    # floored -- so it varies with each ticker's own continuous-mixture
    # dispersion (sigma) instead of being dominated by the atom. See
    # ``_expected_log_moic_below_quantile_given_survival``'s docstring.
    expected_log_moic_below_p10_given_survival = (
        _expected_log_moic_below_quantile_given_survival(0.10, scenarios)
    )
    expected_shortfall_10pct_log_given_survival = (
        expected_log_moic_below_p10_given_survival / horizon_years
    )

    # P(CAGR > r) == P(W_H > (1+r)^H) -- threshold computed from the actual
    # horizon every time (audit §5.2/§6.1: "thresholdはhorizonから計算し、
    # 定数を埋め込まない"), never a hardcoded MOIC constant.
    p_cagr_above = {
        rate: exceed((1.0 + rate) ** horizon_years) for rate in (0.15, 0.20, 0.25)
    }

    # Permanent loss: the audit (§4.3, §5.3, §6.2) is explicit that this
    # must never be fabricated as 0 from the terminal-only distribution
    # above -- it requires a cause-specific competing-risk + recovery-rate
    # model (delisting cause/settlement backfill, not yet collected: WP-F2,
    # still blocked on 94/94 unknown-cause delisting_events rows). Reported
    # as None + an explicit machine-readable reason so no consumer can
    # mistake "not implemented" for "zero risk measured".
    _competing_risk_reason = "competing_risk_model_not_implemented"

    # WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): drawdown/recovery are
    # no longer computed from this terminal-wealth-only distribution at all
    # -- they come from ``path_risk`` (a ``path_risk.PathRiskResult``
    # estimated from the ticker's own realized price history, passed in by
    # the caller). See ``_path_risk_fields`` below.
    path_risk_fields = _path_risk_contract_fields(path_risk, horizon_years=horizon_years)

    return {
        "contract_version": "v5.racr3", "status": "available",
        "distribution_family": "failure_atom_plus_scenario_lognormal_mixture",
        "source_model_version": "v4_structural_seed",
        "target_moic": target_moic,
        "p_moic_below_0_5": p_moic_below_0_5,
        "p_moic_below_1_0": _cdf(1.0, scenarios, survival),
        "p_moic_2x": exceed(2.0), "p_moic_3x": exceed(3.0),
        "p_moic_5x": exceed(5.0), "p_moic_10x": exceed(10.0),
        "p_target": exceed(target_moic),
        "expected_moic": expected_moic, "median_moic": median,
        "expected_cagr": expected_cagr, "median_cagr": median_cagr,
        "expected_shortfall_10pct": _expected_shortfall(0.10, scenarios, survival),
        "p10_moic": quantiles[0.10], "p25_moic": quantiles[0.25],
        "p50_moic": median, "p75_moic": quantiles[0.75],
        "p90_moic": quantiles[0.90],
        "survival_probability": survival,
        "acquisition_probability": None,
        "model_confidence": confidence,
        "scenarios": [s.to_dict() for s in scenarios],
        "expected_moic_given_loss": _conditional_expected_moic_below(
            1.0, scenarios, survival
        ),
        "reliability_sigma_multiplier": sigma_multiplier,
        "reliability_left_tail_extra": left_tail_extra,
        # -- WP-B additions below: purely additive, no existing key above
        # changes value or meaning. --
        "ce_cagr": ce_cagr,
        "ce_cagr_failure_floor": CE_CAGR_FAILURE_FLOOR_MOIC,
        "p_cagr_above_15": p_cagr_above[0.15],
        "p_cagr_above_20": p_cagr_above[0.20],
        "p_cagr_above_25": p_cagr_above[0.25],
        "expected_shortfall_10pct_log": expected_shortfall_10pct_log,
        "expected_shortfall_10pct_log_given_survival": (
            expected_shortfall_10pct_log_given_survival
        ),
        # Rename of p_moic_below_0_5 (audit §5.3/§6.2): "large-principal-
        # impairment probability", explicitly *not* permanent loss. The old
        # key is kept unchanged above for backward compatibility.
        "p_terminal_wealth_below_0_5": p_moic_below_0_5,
        "p_permanent_loss": None,
        "p_permanent_loss_unavailable_reason": _competing_risk_reason,
        # -- WP-F1 additions below: purely additive, no existing key above
        # changes value or meaning. --
        **path_risk_fields,
    }


def _path_risk_contract_fields(path_risk: PathRiskResult | None, *, horizon_years: int) -> dict:
    """Project a ``path_risk.PathRiskResult`` (or its absence) onto the
    distribution contract's drawdown/recovery keys.

    Kept as its own function (rather than inlined into ``scenario_distribution``)
    so the "not provided" vs. "provided but unavailable" vs. "available"
    three-way split is expressed once and cannot drift between the
    ``available`` and future callers of this helper.
    """
    method = "block_bootstrap_weekly_v1"
    if path_risk is None:
        reason = _PATH_RISK_NOT_PROVIDED_REASON
        return {
            "expected_max_drawdown": None, "expected_max_drawdown_unavailable_reason": reason,
            "p_mdd_above_30": None, "p_mdd_above_30_unavailable_reason": reason,
            "p_mdd_above_50": None, "p_mdd_above_50_unavailable_reason": reason,
            "p_mdd_above_70": None, "p_mdd_above_70_unavailable_reason": reason,
            "expected_drawdown_excess_35": None,
            "expected_drawdown_excess_35_unavailable_reason": reason,
            "recovery_time_median": None, "recovery_time_median_unavailable_reason": reason,
            "recovery_time_p90": None, "recovery_time_p90_unavailable_reason": reason,
            "path_risk_method": None, "path_risk_horizon_years": horizon_years,
            "path_risk_observations_used": 0, "path_risk_simulations": 0,
        }
    if path_risk.status != "available":
        reason = path_risk.unavailable_reason
        return {
            "expected_max_drawdown": None, "expected_max_drawdown_unavailable_reason": reason,
            "p_mdd_above_30": None, "p_mdd_above_30_unavailable_reason": reason,
            "p_mdd_above_50": None, "p_mdd_above_50_unavailable_reason": reason,
            "p_mdd_above_70": None, "p_mdd_above_70_unavailable_reason": reason,
            "expected_drawdown_excess_35": None,
            "expected_drawdown_excess_35_unavailable_reason": reason,
            "recovery_time_median": None, "recovery_time_median_unavailable_reason": reason,
            "recovery_time_p90": None, "recovery_time_p90_unavailable_reason": reason,
            "path_risk_method": method, "path_risk_horizon_years": horizon_years,
            "path_risk_observations_used": path_risk.observations_used,
            "path_risk_simulations": 0,
        }
    recovery_reason = path_risk.recovery_time_unavailable_reason
    return {
        "expected_max_drawdown": path_risk.expected_max_drawdown,
        "expected_max_drawdown_unavailable_reason": None,
        "p_mdd_above_30": path_risk.p_mdd_above_30, "p_mdd_above_30_unavailable_reason": None,
        "p_mdd_above_50": path_risk.p_mdd_above_50, "p_mdd_above_50_unavailable_reason": None,
        "p_mdd_above_70": path_risk.p_mdd_above_70, "p_mdd_above_70_unavailable_reason": None,
        "expected_drawdown_excess_35": path_risk.dd_excess,
        "expected_drawdown_excess_35_unavailable_reason": None,
        "recovery_time_median": path_risk.recovery_time_median_days,
        "recovery_time_median_unavailable_reason": recovery_reason,
        "recovery_time_p90": path_risk.recovery_time_p90_days,
        "recovery_time_p90_unavailable_reason": recovery_reason,
        "path_risk_method": method, "path_risk_horizon_years": horizon_years,
        "path_risk_observations_used": path_risk.observations_used,
        "path_risk_simulations": path_risk.simulations,
    }
