"""Pure accounting-quality calculations for display and review."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountingQuality:
    accrual_ratio: float | None
    cash_conversion: float | None
    receivables_gap: float | None
    inventory_gap: float | None
    sbc_to_revenue: float | None
    goodwill_to_assets: float | None
    warnings: list[str] = field(default_factory=list)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def calculate_accounting_quality(
    *, net_income: float | None, operating_cash_flow: float | None,
    average_assets: float | None, revenue_growth: float | None,
    receivables_growth: float | None, inventory_growth: float | None,
    stock_based_compensation: float | None, revenue: float | None,
    goodwill: float | None, total_assets: float | None,
) -> AccountingQuality:
    accrual = _ratio(
        None if net_income is None or operating_cash_flow is None else net_income - operating_cash_flow,
        average_assets,
    )
    conversion = _ratio(operating_cash_flow, net_income)
    receivables_gap = (
        receivables_growth - revenue_growth
        if receivables_growth is not None and revenue_growth is not None else None
    )
    inventory_gap = (
        inventory_growth - revenue_growth
        if inventory_growth is not None and revenue_growth is not None else None
    )
    sbc = _ratio(stock_based_compensation, revenue)
    goodwill_ratio = _ratio(goodwill, total_assets)
    warnings: list[str] = []
    if accrual is not None and accrual > 0.10: warnings.append("high_accruals")
    if conversion is not None and conversion < 0.80: warnings.append("weak_cash_conversion")
    if receivables_gap is not None and receivables_gap > 0.10: warnings.append("receivables_outpacing_revenue")
    if inventory_gap is not None and inventory_gap > 0.15: warnings.append("inventory_outpacing_revenue")
    if sbc is not None and sbc > 0.15: warnings.append("high_sbc_to_revenue")
    if goodwill_ratio is not None and goodwill_ratio > 0.50: warnings.append("high_goodwill_to_assets")
    return AccountingQuality(accrual, conversion, receivables_gap, inventory_gap, sbc, goodwill_ratio, warnings)
