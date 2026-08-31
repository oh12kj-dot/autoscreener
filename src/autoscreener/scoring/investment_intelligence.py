"""Pure display/portfolio-layer metrics introduced by TENX v2."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import NormalDist


def cagr(start: float | None, end: float | None, years: float) -> float | None:
    if start is None or end is None or start <= 0 or end < 0 or years <= 0:
        return None
    return (end / start) ** (1 / years) - 1


@dataclass(frozen=True)
class ReinvestmentQuality:
    incremental_roic: float | None
    reinvestment_rate: float | None
    revenue_cagr: float | None
    revenue_per_share_cagr: float | None
    gross_profit_cagr: float | None
    gross_profit_per_share_cagr: float | None
    fcf_cagr: float | None
    fcf_per_share_cagr: float | None


def calculate_reinvestment_quality(*, years: float, revenue_start: float | None,
    revenue_end: float | None, gross_profit_start: float | None, gross_profit_end: float | None,
    fcf_start: float | None, fcf_end: float | None, shares_start: float | None,
    shares_end: float | None, nopat_start: float | None, nopat_end: float | None,
    invested_capital_start: float | None, invested_capital_end: float | None) -> ReinvestmentQuality:
    delta_ic = None if invested_capital_start is None or invested_capital_end is None else invested_capital_end - invested_capital_start
    delta_nopat = None if nopat_start is None or nopat_end is None else nopat_end - nopat_start
    incremental_roic = delta_nopat / delta_ic if delta_ic is not None and delta_nopat is not None and delta_ic > 0 else None
    reinvestment = delta_ic / nopat_end if delta_ic is not None and nopat_end is not None and nopat_end > 0 else None
    per_share = lambda value, shares: value / shares if value is not None and shares is not None and shares > 0 else None
    return ReinvestmentQuality(
        incremental_roic, reinvestment, cagr(revenue_start, revenue_end, years),
        cagr(per_share(revenue_start, shares_start), per_share(revenue_end, shares_end), years),
        cagr(gross_profit_start, gross_profit_end, years),
        cagr(per_share(gross_profit_start, shares_start), per_share(gross_profit_end, shares_end), years),
        cagr(fcf_start, fcf_end, years), cagr(per_share(fcf_start, shares_start), per_share(fcf_end, shares_end), years),
    )


def return_distribution(log_mu: float, log_sigma: float, survival_probability: float, horizon_years: float) -> dict[str, float]:
    nd = NormalDist()
    def p_moic(threshold: float) -> float:
        if threshold <= 0: return survival_probability
        return survival_probability * (1 - nd.cdf((math.log(threshold) - log_mu) / log_sigma))
    expected_moic = survival_probability * math.exp(log_mu + 0.5 * log_sigma**2)
    median_moic = 0.0 if survival_probability <= 0.5 else math.exp(log_mu + log_sigma * nd.inv_cdf(1 - 0.5 / survival_probability))
    result = {f"p_cagr_{pct}": p_moic((1 + pct / 100) ** horizon_years) for pct in (10, 15, 20, 25)}
    lower_tail: list[float] = []
    for index in range(1, 201):
        u = 0.10 * (index - 0.5) / 200
        if u <= 1 - survival_probability:
            lower_tail.append(-1.0)
        else:
            conditional = (u - (1 - survival_probability)) / survival_probability
            lower_tail.append(math.exp(log_mu + log_sigma * nd.inv_cdf(conditional)) - 1)
    result.update({"p_moic_2x": p_moic(2), "p_moic_3x": p_moic(3),
        "expected_cagr": expected_moic ** (1 / horizon_years) - 1,
        "median_cagr": -1.0 if median_moic == 0 else median_moic ** (1 / horizon_years) - 1,
        "expected_shortfall_10pct": sum(lower_tail) / len(lower_tail)})
    return result


def macro_exposure(asset_returns: list[float], factor_returns: list[float]) -> dict[str, float | int | None]:
    """OLS beta and downside beta on aligned returns; association, not causality."""
    pairs = [(a, f) for a, f in zip(asset_returns, factor_returns) if math.isfinite(a) and math.isfinite(f)]
    def beta(values: list[tuple[float, float]]) -> float | None:
        if len(values) < 3:
            return None
        mean_a = sum(a for a, _ in values) / len(values)
        mean_f = sum(f for _, f in values) / len(values)
        variance = sum((f - mean_f) ** 2 for _, f in values)
        if variance == 0:
            return None
        return sum((a - mean_a) * (f - mean_f) for a, f in values) / variance
    return {"beta": beta(pairs), "downside_beta": beta([(a, f) for a, f in pairs if f < 0]), "sample_count": len(pairs)}


@dataclass(frozen=True)
class RiskSizingPreview:
    base_cap: float
    vol_factor: float
    correlation_factor: float
    sector_factor: float
    uncertainty_factor: float
    recommended_cap: float


def risk_sizing_preview(*, per_position_cap: float, liquidity_cap: float,
    realized_vol: float | None, target_vol: float, correlation_factor: float = 1.0,
    sector_factor: float = 1.0, uncertainty_factor: float = 1.0,
    min_vol_factor: float = 0.35) -> RiskSizingPreview:
    base = min(per_position_cap, liquidity_cap)
    vol = 1.0 if realized_vol is None or realized_vol <= 0 else max(min_vol_factor, min(1.0, target_vol / realized_vol))
    factors = [max(0.0, min(1.0, x)) for x in (correlation_factor, sector_factor, uncertainty_factor)]
    return RiskSizingPreview(base, vol, *factors, base * vol * math.prod(factors))


def jpy_after_tax_return(*, usd_moic: float, entry_usdjpy: float, exit_usdjpy: float,
    account_type: str = "taxable", capital_gain_tax_rate: float = 0.20315,
    fx_spread_bps: float = 0.0, brokerage_fee_bps: float = 0.0, horizon_years: float = 7.0) -> dict[str, float]:
    costs = (fx_spread_bps + brokerage_fee_bps) / 10_000
    jpy_pre_tax = usd_moic * exit_usdjpy / entry_usdjpy * (1 - costs)
    tax = 0.0 if account_type.upper() == "NISA" else max(0.0, jpy_pre_tax - 1) * capital_gain_tax_rate
    after = max(0.0, jpy_pre_tax - tax)
    return {"usd_pre_tax_moic": usd_moic, "jpy_pre_tax_moic": jpy_pre_tax,
        "jpy_after_tax_moic": after, "annualized_irr": after ** (1/horizon_years)-1,
        "break_even_usdjpy": entry_usdjpy / max(usd_moic * (1-costs), 1e-12)}
