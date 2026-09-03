"""Central contracts for every signal that may enter Model v5."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    source: str
    target_state: str
    direction: str
    transform: str
    winsorization: tuple[float, float] | None
    sector_normalization: bool
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
        if self.winsorization is not None:
            low, high = self.winsorization
            if not 0 <= low < high <= 1:
                raise ValueError(f"{self.key}: invalid winsorization quantiles")

    def to_dict(self) -> dict:
        return asdict(self)


def _spec(
    key: str,
    source: str,
    target_state: str,
    *,
    direction: str = "context_dependent",
    transform: str = "identity",
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
        transform=transform,
        winsorization=(0.01, 0.99) if transform == "robust_z" else None,
        sector_normalization=transform == "robust_z",
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
          transform="bounded_within_company_change", required_coverage=0.50,
          notes="Comparable family-specific KPI observations update near-term growth."),
    _spec("consensus_revision", "analyst_consensus_snapshots", "revenue_growth_path",
          transform="bounded_same_period_revision", required_coverage=0.80, freshness_days=90,
          notes="Same-period revision is a bounded observation update, never ground truth."),
    _spec("guidance", "management_guidance_snapshots", "revenue_growth_path", freshness_days=180,
          required_coverage=0.50, notes="Validated revenue guidance only; missing guidance remains neutral."),
    _spec("incremental_roic", "raw_snapshots_financial_history", "growth_duration", direction="higher_better",
          transform="robust_z", enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: ANOPAT/AIC only shortens duration when growth is high and ROIC is below "
                "the config hurdle; never extends duration on its own."),
    _spec("per_share_economics", "raw_snapshots_financial_history", "growth_mean_multiplier",
          direction="lower_better", transform="robust_z", enabled=True, required_coverage=0.90,
          min_reliability=0.5,
          notes="Phase 4: gross-profit/FCF per-share vs total CAGR gap decays the growth mean "
                "multiplier; deliberately excludes revenue to avoid double-counting v4's "
                "capital.diluted_share_factor (dilution_drag)."),
    _spec("cash_conversion", "raw_snapshots_financial_history", "economics_cash_conversion",
          transform="identity", enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: fills economics.cash_conversion / reinvestment_efficiency (OCF/NI, "
                "FCF/NI) which Phase 2/3 left unsupported. Diagnostic state only; no distribution "
                "multiplier."),
    _spec("accounting_quality", "raw_snapshots_financial_history", "uncertainty", direction="higher_better",
          transform="robust_z", enabled=True, required_coverage=0.90, min_reliability=0.5,
          notes="Phase 4: accrual ratio / weak cash conversion / receivables & inventory gap / "
                "SBC / goodwill severity widens sigma and the left tail only; the conditional "
                "mean is never lowered (Issue #3 section 6.3)."),
    _spec("reconciliation_confidence", "xbrl_facts", "uncertainty_confidence",
          transform="identity", enabled=True, required_coverage=0.80, min_reliability=0.5,
          notes="Phase 4: yfinance-vs-SEC-XBRL mismatch/magnitude_mismatch only lowers "
                "model_confidence; the state is never moved."),
    _spec("capital_allocation", "capital_allocation_events", "capital_allocation_path",
          notes="Event-to-state mapping is introduced in Phase 5."),
    _spec("debt_maturity", "debt_instruments", "refinancing_survival",
          notes="Maturity walls affect survival and left-tail scenarios."),
    _spec("liquidity", "liquidity_facilities", "refinancing_survival", direction="higher_better",
          notes="Separates short-term liquidity from long-term leverage."),
    _spec("customer_concentration", "customer_concentrations", "tail_risk",
          notes="Concentration widens idiosyncratic left tail."),
    _spec("litigation", "litigation_events", "tail_risk", historical=False,
          notes="Shadow only until severity and amount coverage are reliable."),
    _spec("macro_regime", "macro_exposure_snapshots", "scenario_distribution", historical=False,
          notes="FRED current observations cannot be used in historical reconstruction."),
    _spec("acquisition_competing_risk", "delisting_events", "competing_risk", historical=False,
          notes="Disabled while event classification coverage is below threshold."),
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
