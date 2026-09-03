"""Typed future-state contract for Model v5.

Phase 2 materialises every state named by Issue #3 while keeping later-phase
signals explicitly unsupported. A missing state is never converted to zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from autoscreener.scoring.moic import MoicInputs, MoicResult
from autoscreener.scoring.v5.growth import GrowthUpdate


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
    return FutureState(
        contract_version=contract_version, status=status,
        growth=GrowthState(
            StateValue(initial_rate, "updated" if state_updates else "seed", "v5_growth_state" if state_updates else "v4_structural_model"),
            _seed(result.terminal_growth_rate), duration,
            StateValue(revenue_multiple, "updated" if state_updates else "seed", "v5_growth_state" if state_updates else "v4_structural_model"),
        ),
        economics=EconomicsState(
            _seed(result.terminal_gross_margin),
            StateValue(inputs.fcf_margin, "seed" if inputs.fcf_margin is not None else "not_collected", "financial_statements"),
            _unsupported("phase4"),
        ),
        capital=CapitalState(
            _seed(result.dilution_drag), _seed(inputs.net_debt),
            StateValue(result.projected_net_debt, "diagnostic", "v4_structural_model"),
        ),
        valuation=ValuationState(_seed(result.current_ev_to_gross_profit), _seed(terminal_multiple)),
        competing_risk=CompetingRiskState(
            _seed(result.survival_probability), _unsupported("phase6"), _unsupported("phase6"),
        ),
        uncertainty=UncertaintyState(
            _seed(result.log_moic_sigma),
            StateValue(confidence, "measured", "input_reliability"),
            "scenario_lognormal_mixture",
        ),
        state_updates_applied=state_updates,
    )
