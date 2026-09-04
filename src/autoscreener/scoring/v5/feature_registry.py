"""Central contracts for every signal that may enter Model v5.

WP-D D-3 (docs/racr_wp_d_reliability_layer_2026-09-04.md): ``transform``,
``winsorization``, and ``sector_normalization`` were removed from
``FeatureSpec`` here (2026-09-04) after being verified to have zero runtime
references outside this module -- pure declared-but-dead metadata, exactly
what the audit's "registry記載だけのtransform/sector normalization" finding
objects to. They are not wired to real execution in this WP because doing
so honestly requires re-deriving each signal's units (several signals
already compute a hurdle-relative "shortfall" or a bounded ratio, not a raw
observation -- z-scoring or winsorizing *that* derived quantity is a
different, larger modeling decision than the audit's registry entry
implies, and belongs with the population-wide, sector-aware feature
DAG/correlation work the plan itself schedules as its own file
(`scoring/v5/feature_graph.py`, P3 in the redesign plan) rather than a
shallow bolt-on here that could silently change every downstream formula's
meaning. See the WP-D doc for the full reasoning.

``freshness_half_life_days`` is the opposite case -- it now genuinely
executes (``reliability.decayed_reliability``, wired into every
``build_*_feature_sets`` assembly loop) -- so it is kept.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    source: str
    target_state: str
    direction: str
    required_coverage: float
    freshness_half_life_days: int | None
    pit_required: bool
    historical_backtest_supported: bool
    min_reliability: float
    model_families: tuple[str, ...]
    default_enabled: bool
    notes: str

    def __post_init__(self) -> None:
        if not 0 <= self.required_coverage <= 1:
            raise ValueError(f"{self.key}: required_coverage must be in [0,1]")
        if not 0 <= self.min_reliability <= 1:
            raise ValueError(f"{self.key}: min_reliability must be in [0,1]")

    def to_dict(self) -> dict:
        return asdict(self)


def _spec(
    key: str,
    source: str,
    target_state: str,
    *,
    direction: str = "context_dependent",
    historical: bool = True,
    enabled: bool = False,
    required_coverage: float = 0.0,
    freshness_days: int | None = None,
    min_reliability: float = 0.6,
    notes: str,
) -> FeatureSpec:
    return FeatureSpec(
        key=key,
        source=source,
        target_state=target_state,
        direction=direction,
        required_coverage=required_coverage,
        freshness_half_life_days=freshness_days,
        pit_required=True,
        historical_backtest_supported=historical,
        min_reliability=min_reliability,
        model_families=("general_corporate",),
        default_enabled=enabled,
        notes=notes,
    )


FEATURE_REGISTRY: tuple[FeatureSpec, ...] = (
    _spec("base_financial_statements", "raw_snapshots", "base_distribution", enabled=True,
          notes="Phase 1 structural seed; filtered by available_from and reporting lag."),
    _spec("price_history", "price_snapshots", "base_distribution", enabled=True,
          notes="Only trade_date <= as_of is visible."),
    _spec("tam_headroom", "market_opportunity_estimates", "growth_duration", direction="higher_better",
          required_coverage=0.50, notes="Caps duration only; a large TAM is not a direct positive score."),
    _spec("operating_kpi_nowcast", "operating_kpi_observations", "revenue_growth_path",
          required_coverage=0.50,
          notes="Comparable family-specific KPI observations update near-term growth."),
    _spec("consensus_revision", "analyst_consensus_snapshots", "revenue_growth_path",
          required_coverage=0.80, freshness_days=90,
          notes="Same-period revision is a bounded observation update, never ground truth."),
    _spec("guidance", "management_guidance_snapshots", "revenue_growth_path", freshness_days=180,
          required_coverage=0.50, notes="Validated revenue guidance only; missing guidance remains neutral."),
    _spec("incremental_roic", "raw_snapshots_financial_history", "growth_duration", direction="higher_better",
          enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: ANOPAT/AIC only shortens duration when growth is high and ROIC is below "
                "the config hurdle; never extends duration on its own."),
    _spec("per_share_economics", "raw_snapshots_financial_history", "growth_mean_multiplier",
          direction="lower_better", enabled=True, required_coverage=0.90,
          min_reliability=0.5,
          notes="Phase 4: gross-profit/FCF per-share vs total CAGR gap decays the growth mean "
                "multiplier; deliberately excludes revenue to avoid double-counting v4's "
                "capital.diluted_share_factor (dilution_drag)."),
    _spec("cash_conversion", "raw_snapshots_financial_history", "economics_cash_conversion",
          enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: fills economics.cash_conversion / reinvestment_efficiency (OCF/NI, "
                "FCF/NI) which Phase 2/3 left unsupported. Diagnostic state only; no distribution "
                "multiplier."),
    _spec("accounting_quality", "raw_snapshots_financial_history", "uncertainty", direction="higher_better",
          enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: accrual ratio / weak cash conversion / receivables & inventory gap / "
                "SBC / goodwill severity widens sigma and the left tail only; the conditional "
                "mean is never lowered (Issue #3 section 6.3)."),
    _spec("reconciliation_confidence", "xbrl_facts", "uncertainty_confidence",
          enabled=True, required_coverage=0.80, min_reliability=0.5,
          notes="Phase 4: yfinance-vs-SEC-XBRL mismatch/magnitude_mismatch only lowers "
                "model_confidence; the state is never moved."),
    _spec("capital_allocation", "capital_allocation_events", "refinancing_survival",
          direction="lower_better", enabled=True,
          required_coverage=0.50, min_reliability=0.5,
          notes="Phase 5: trailing-window committed cash return (buyback+dividend) net of "
                "raised capital (debt_raise+equity_raise), relative to cash balance. Reads "
                "only already-announced events in a bounded window -- never extrapolates a "
                "historical buyback rate forward (Issue #3 section 7)."),
    _spec("debt_maturity", "debt_instruments", "refinancing_survival",
          direction="lower_better", enabled=True,
          required_coverage=0.50, min_reliability=0.5,
          notes="Phase 5: 12-month debt maturity wall (routes.py:3619's due_12m definition, "
                "reused) vs cash + revolver_available. Only ever shortens survival_probability "
                "(Phase 2/3/4 held it fixed); never grants a bonus for being well covered."),
    _spec("liquidity", "liquidity_facilities", "refinancing_survival", direction="higher_better",
          enabled=True, required_coverage=0.50, min_reliability=0.5,
          notes="Phase 5: cash runway from the latest annual FCF burn, separate from the "
                "long-term-leverage-driven debt_maturity signal above."),
    _spec("future_dilution_capacity", "dilution_capacity", "growth_mean_multiplier",
          direction="lower_better", enabled=True,
          required_coverage=0.50, min_reliability=0.5,
          notes="Phase 6 (Issue #3 section 12, user decision 2026-09-03): ATM/shelf remaining "
                "authorization + unexercised options/warrants + variable-conversion flag decay "
                "the growth mean multiplier -- unissued future capacity, distinct from v4's "
                "dilution_drag and Phase 4's per_share_economics (both realized/historical). "
                "Shares an explicit anti-triple-counting reduction budget with those two "
                "(config.capital.max_combined_dilution_reduction)."),
    _spec("customer_concentration", "customer_concentration", "tail_risk",
          direction="lower_better", enabled=True,
          required_coverage=0.50, min_reliability=0.5,
          notes="Phase 6: total disclosed 10%+ customer revenue concentration widens the left "
                "tail only (Issue #3 section 12: never lowers the mean growth rate directly)."),
    _spec("litigation", "litigation_events", "tail_risk", direction="lower_better",
          historical=False, enabled=True, required_coverage=0.50,
          min_reliability=0.5,
          notes="Phase 6: shadow only until severity/amount coverage is reliable -- the table "
                "has no severity/amount field at all yet, only kind/title/detail text; trailing-"
                "window event count is used as an explicitly bounded, crude proxy for the left "
                "tail only."),
    _spec("macro_regime", "macro_exposure_snapshots", "scenario_distribution",
          direction="lower_better", historical=False,
          enabled=True, required_coverage=0.50, min_reliability=0.5,
          notes="Phase 6: downside_beta widens the left tail only, and only when "
                "fred_vintage_supported=true (0% of current rows) -- FRED current observations "
                "are never used in historical reconstruction. High beta/exposure alone is not "
                "treated as bad (Issue #3 section 10)."),
    _spec("acquisition_competing_risk", "delisting_events", "competing_risk", historical=False,
          notes="Phase 6: deliberately NOT implemented. 94/94 delisting_events rows are "
                "event_type=unknown (Phase 0 baseline), below any defensible classification-"
                "coverage threshold; Issue #3 section 13 explicitly prohibits treating unknown "
                "as acquisition=0. competing_risk.acquisition_probability stays unsupported."),
)

FEATURES_BY_KEY = {feature.key: feature for feature in FEATURE_REGISTRY}

if len(FEATURES_BY_KEY) != len(FEATURE_REGISTRY):
    raise RuntimeError("Model v5 feature keys must be unique")


def validate_feature_flags(flags: dict[str, bool]) -> None:
    unknown = sorted(set(flags) - set(FEATURES_BY_KEY))
    if unknown:
        raise ValueError(f"unknown Model v5 feature flags: {', '.join(unknown)}")


def feature_registry_payload(flags: dict[str, bool]) -> list[dict]:
    validate_feature_flags(flags)
    payload = []
    for spec in FEATURE_REGISTRY:
        item = spec.to_dict()
        item["enabled"] = flags.get(spec.key, spec.default_enabled)
        payload.append(item)
    return payload
