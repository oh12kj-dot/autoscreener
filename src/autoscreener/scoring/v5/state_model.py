"""Typed future-state contract for Model v5.

Phase 2 materialises every state named by Issue #3 while keeping later-phase
signals explicitly unsupported. A missing state is never converted to zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from autoscreener.scoring.moic import MoicInputs, MoicResult
from autoscreener.scoring.v5.balance_sheet import CapitalUpdate
from autoscreener.scoring.v5.growth import GrowthUpdate
from autoscreener.scoring.v5.quality import QualityUpdate


@dataclass(frozen=True)
class StateValue:
    value: float | None
    status: str
    source: str | None = None


@dataclass(frozen=True)
class GrowthState:
    initial_rate: StateValue
    terminal_rate: StateValue
    duration_years: StateValue
    revenue_multiple: StateValue


@dataclass(frozen=True)
class EconomicsState:
    terminal_margin: StateValue
    cash_conversion: StateValue
    reinvestment_efficiency: StateValue


@dataclass(frozen=True)
class CapitalState:
    diluted_share_factor: StateValue
    current_net_debt: StateValue
    projected_net_debt: StateValue


@dataclass(frozen=True)
class ValuationState:
    current_multiple: StateValue
    terminal_multiple: StateValue


@dataclass(frozen=True)
class CompetingRiskState:
    survival_probability: StateValue
    acquisition_probability: StateValue
    other_exit_probability: StateValue


@dataclass(frozen=True)
class UncertaintyState:
    seed_log_sigma: StateValue
    model_confidence: StateValue
    tail_model: str


@dataclass(frozen=True)
class FutureState:
    contract_version: str
    status: str
    growth: GrowthState
    economics: EconomicsState
    capital: CapitalState
    valuation: ValuationState
    competing_risk: CompetingRiskState
    uncertainty: UncertaintyState
    state_updates_applied: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def _seed(value: float | None) -> StateValue:
    return StateValue(value=value, status="seed", source="v4_structural_model")


def _unsupported(source: str) -> StateValue:
    return StateValue(value=None, status="unsupported", source=source)


def build_future_state(
    result: MoicResult | None,
    inputs: MoicInputs | None,
    *,
    horizon_years: int,
    confidence: float,
    growth_update: GrowthUpdate | None = None,
    quality_update: QualityUpdate | None = None,
    capital_update: CapitalUpdate | None = None,
    contract_version: str = "v5.phase2",
) -> FutureState:
    """Build the complete state namespace without inventing later-phase data."""
    if result is None or inputs is None:
        missing = StateValue(value=None, status="unavailable", source=None)
        return FutureState(
            contract_version=contract_version, status="unavailable",
            growth=GrowthState(missing, missing, missing, missing),
            economics=EconomicsState(missing, missing, _unsupported("phase4")),
            capital=CapitalState(missing, missing, missing),
            valuation=ValuationState(missing, missing),
            competing_risk=CompetingRiskState(missing, _unsupported("phase6"), _unsupported("phase6")),
            uncertainty=UncertaintyState(missing, StateValue(confidence, "measured", "input_reliability"), "scenario_lognormal_mixture"),
        )

    terminal_multiple = result.current_ev_to_gross_profit * result.multiple_change
    initial_rate = result.initial_growth_rate
    duration = _unsupported("phase3")
    revenue_multiple = result.revenue_multiple
    state_updates: tuple[str, ...] = ()
    status = "seeded"
    if growth_update is not None:
        initial_rate = growth_update.updated_initial_rate
        duration = StateValue(
            growth_update.updated_duration_years, "updated", "v5_growth_state"
        )
        revenue_multiple *= growth_update.revenue_multiple_ratio
        state_updates = growth_update.applied_keys
        status = "updated" if state_updates else "seeded"

    # Phase 4 (docs/model_v5_phase4_handoff_2026-09-03.md 4.3): incremental
    # ROIC only ever shortens the already-computed duration state, and
    # per_share_economics only ever decays the already-computed revenue
    # multiple. Neither can extend duration or amplify the multiple beyond
    # what growth already established.
    if quality_update is not None and quality_update.duration_multiplier < 1.0 and duration.value is not None:
        duration = StateValue(
            duration.value * quality_update.duration_multiplier, "updated", "v5_quality_state"
        )
    if quality_update is not None:
        revenue_multiple *= quality_update.mean_multiplier
    if quality_update is not None and quality_update.applied_keys:
        state_updates = tuple(state_updates) + quality_update.applied_keys
        status = "updated"

    cash_conversion_effect = (
        quality_update.signal_effects.get("cash_conversion") if quality_update is not None else None
    )
    if cash_conversion_effect is not None:
        cash_conversion = StateValue(
            cash_conversion_effect.get("cash_conversion"), "updated", "v5_quality_state"
        )
        reinvestment_efficiency = (
            StateValue(cash_conversion_effect.get("reinvestment_efficiency"), "updated", "v5_quality_state")
            if cash_conversion_effect.get("reinvestment_efficiency") is not None
            else StateValue(None, "not_collected", "v5_quality_state")
        )
    elif quality_update is not None:
        # Phase 4 quality machinery ran for this ticker, but the
        # cash_conversion signal itself was not applied this run (coverage
        # gate / near-zero net income / no financial history) -- genuinely
        # missing data, not an unimplemented phase.
        cash_conversion = StateValue(
            inputs.fcf_margin, "seed" if inputs.fcf_margin is not None else "not_collected", "financial_statements"
        )
        reinvestment_efficiency = StateValue(None, "not_collected", "v5_quality_state")
    else:
        cash_conversion = StateValue(
            inputs.fcf_margin, "seed" if inputs.fcf_margin is not None else "not_collected", "financial_statements"
        )
        reinvestment_efficiency = _unsupported("phase4")

    # Phase 5 (docs/model_v5_phase5_capital_allocation_2026-09-03.md):
    # debt-maturity/liquidity/capital-allocation shortfalls only ever shrink
    # survival_probability below the v4 seed (never a bonus) -- the first
    # phase to move it; Phase 2/3/4 held it fixed.
    survival_value = result.survival_probability
    if capital_update is not None and capital_update.applied_keys:
        survival_value = result.survival_probability * capital_update.survival_multiplier
        state_updates = tuple(state_updates) + capital_update.applied_keys
        status = "updated"
    survival = StateValue(
        survival_value,
        "updated" if capital_update is not None and capital_update.applied_keys else "seed",
        "v5_capital_state" if capital_update is not None and capital_update.applied_keys else "v4_structural_model",
    )

    return FutureState(
        contract_version=contract_version, status=status,
        growth=GrowthState(
            StateValue(initial_rate, "updated" if state_updates else "seed", "v5_growth_state" if state_updates else "v4_structural_model"),
            _seed(result.terminal_growth_rate), duration,
            StateValue(revenue_multiple, "updated" if state_updates else "seed", "v5_growth_state" if state_updates else "v4_structural_model"),
        ),
        economics=EconomicsState(
            _seed(result.terminal_gross_margin),
            cash_conversion,
            reinvestment_efficiency,
        ),
        capital=CapitalState(
            _seed(result.dilution_drag), _seed(inputs.net_debt),
            StateValue(result.projected_net_debt, "diagnostic", "v4_structural_model"),
        ),
        valuation=ValuationState(_seed(result.current_ev_to_gross_profit), _seed(terminal_multiple)),
        competing_risk=CompetingRiskState(
            survival, _unsupported("phase6"), _unsupported("phase6"),
        ),
        uncertainty=UncertaintyState(
            _seed(result.log_moic_sigma),
            StateValue(confidence, "measured", "input_reliability"),
            "scenario_lognormal_mixture",
        ),
        state_updates_applied=state_updates,
    )
