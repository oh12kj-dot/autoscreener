"""財務推移の表示用ビュー(J-2、investment_decision_gap_2026-08-29.md)。

**表示層のためだけのモジュール。** スコアリング・ゲートからは import しない
(順位を動かしてはならない。J-2 受け入れ基準)。`raw_snapshots.payload` に既に
入っている財務三表を、人間が「売上・粗利率・現金・株式数がどう推移してきたか」を
読むために整形するだけ。

再実装を避けるため、行名の表記ゆれ対応が既にある
`scoring.point_in_time` / `screening.exclusion_gates` の純関数を再利用する。
表示ではポイントインタイム・フィルタは不要なので、可視期間を全期に開いて
(`as_of = date.max`)呼ぶ。

**通貨**:決算通貨(`financialCurrency`)と取引通貨(`currency`)が異なる ADR
などでは、金額系列を `financial_to_trading_rate` で取引通貨に揃えてから返す
(2026-08-26 の欠陥「通貨混在による EV 誤り」の再発防止)。換算レートが取れない
ときは換算せず `currency_conversion_unavailable=True` を立てる。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from autoscreener.scoring.financial_metrics import PIOTROSKI_CRITERIA, piotroski_f_score
from autoscreener.scoring.point_in_time import (
    _CASH_ROWS,
    build_point_in_time_statements,
    financial_to_trading_rate,
    revenue_cagr,
    revenue_yoy,
)
from autoscreener.screening.exclusion_gates import dilution_cagr_with_window, parse_period_series

# 表示する期数の上限(古い期は情報量が薄く、縦に長くなるだけ)。
_MAX_ANNUAL = 4
_MAX_QUARTERLY = 5

_INCOME_ROWS = {
    "revenue": "Total Revenue",
    "gross_profit": "Gross Profit",
    "operating_income": "Operating Income",
    "net_income": "Net Income",
}
_CASH_FLOW_ROWS = {
    "operating_cash_flow": "Operating Cash Flow",
    "capex": "Capital Expenditure",
    "free_cash_flow": "Free Cash Flow",
}
_TOTAL_DEBT_ROW = "Total Debt"
_SHARES_ROW = "Ordinary Shares Number"

_MONTHS_PER_QUARTER = 3.0


@dataclass(frozen=True)
class FinancialPeriod:
    """1期(年次または四半期)の実績。取れなかった行は None。"""

    period_end: datetime.date
    revenue: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    cash_and_equivalents: float | None = None
    total_debt: float | None = None
    net_debt: float | None = None
    shares_outstanding: float | None = None


@dataclass(frozen=True)
class PiotroskiCriterion:
    key: str
    label: str
    met: bool | None


@dataclass(frozen=True)
class FinancialHistoryDerived:
    revenue_yoy: float | None = None
    revenue_cagr_3y: float | None = None
    gross_margin_latest: float | None = None
    # 四半期FCFの平均バーンレート(1四半期あたり。正の値=毎四半期これだけ現金が
    # 減る)。FCF が黒字なら None(バーンしていない)。
    quarterly_burn_rate: float | None = None
    # 現金 ÷ 月次バーン。FCF が黒字(バーンなし)のときは None(=実質無限)。
    runway_months: float | None = None
    # 比較用に並べるゲート閾値(config/monitoring.yaml)。
    runway_floor_months: float | None = None
    # 発行済株式数の年率増加率(年次系列の実測窓で年率換算)。
    share_growth_rate: float | None = None
    piotroski_score_ratio: float | None = None
    piotroski_criteria_met: int = 0
    piotroski_criteria_computable: int = 0
    piotroski_criteria: list[PiotroskiCriterion] = field(default_factory=list)


@dataclass(frozen=True)
class FinancialHistory:
    currency: str | None
    currency_conversion_unavailable: bool
    annual: list[FinancialPeriod]
    quarterly: list[FinancialPeriod]
    derived: FinancialHistoryDerived
    as_of: datetime.date | None


def _row_map(statement: dict | None, row: str) -> dict[datetime.date, float]:
    """財務諸表の1行を {期末日: 値} にする。行が無ければ空 dict。"""
    if not statement:
        return {}
    return {d: v for d, v in parse_period_series(statement.get(row))}


def _cash_row_map(statement: dict | None) -> dict[datetime.date, float]:
    """現金同等物。`_CASH_ROWS` の優先順で最初に見つかった行を使う。"""
    if not statement:
        return {}
    for row in _CASH_ROWS:
        series = _row_map(statement, row)
        if series:
            return series
    return {}


def _scale(value: float | None, fx: float) -> float | None:
    return None if value is None else value * fx


def _build_periods(
    period_ends: list[datetime.date],
    income_stmt: dict | None,
    balance_sheet: dict | None,
    cash_flow: dict | None,
    fx: float,
    limit: int,
) -> list[FinancialPeriod]:
    revenue = _row_map(income_stmt, _INCOME_ROWS["revenue"])
    gross_profit = _row_map(income_stmt, _INCOME_ROWS["gross_profit"])
    operating_income = _row_map(income_stmt, _INCOME_ROWS["operating_income"])
    net_income = _row_map(income_stmt, _INCOME_ROWS["net_income"])
    ocf = _row_map(cash_flow, _CASH_FLOW_ROWS["operating_cash_flow"])
    capex = _row_map(cash_flow, _CASH_FLOW_ROWS["capex"])
    fcf = _row_map(cash_flow, _CASH_FLOW_ROWS["free_cash_flow"])
    cash = _cash_row_map(balance_sheet)
    total_debt = _row_map(balance_sheet, _TOTAL_DEBT_ROW)
    shares = _row_map(balance_sheet, _SHARES_ROW)

    periods: list[FinancialPeriod] = []
    for period_end in sorted(period_ends)[-limit:]:
        rev = revenue.get(period_end)
        gp = gross_profit.get(period_end)
        margin = gp / rev if rev not in (None, 0) and gp is not None else None
        debt_v = total_debt.get(period_end)
        cash_v = cash.get(period_end)
        net_debt_v = (
            (debt_v - cash_v) if debt_v is not None and cash_v is not None else None
        )
        periods.append(
            FinancialPeriod(
                period_end=period_end,
                revenue=_scale(rev, fx),
                gross_profit=_scale(gp, fx),
                gross_margin=margin,  # 比なので通貨を掛けても不変
                operating_income=_scale(operating_income.get(period_end), fx),
                net_income=_scale(net_income.get(period_end), fx),
                operating_cash_flow=_scale(ocf.get(period_end), fx),
                capex=_scale(capex.get(period_end), fx),
                free_cash_flow=_scale(fcf.get(period_end), fx),
                cash_and_equivalents=_scale(cash_v, fx),
                total_debt=_scale(debt_v, fx),
                net_debt=_scale(net_debt_v, fx),
                shares_outstanding=shares.get(period_end),  # 通貨と無関係
            )
        )
    return periods


def _all_period_ends(*statements: dict | None) -> list[datetime.date]:
    ends: set[datetime.date] = set()
    for statement in statements:
        if not statement:
            continue
        for series in statement.values():
            if isinstance(series, dict):
                for d, _v in parse_period_series(series):
                    ends.add(d)
    return sorted(ends)


def _derived(
    annual: list[FinancialPeriod],
    quarterly: list[FinancialPeriod],
    piotroski,
    runway_floor_months: float | None,
) -> FinancialHistoryDerived:
    revenue_points = [(p.period_end, p.revenue) for p in annual if p.revenue is not None and p.revenue > 0]
    yoy = revenue_yoy(revenue_points) if len(revenue_points) >= 2 else None
    cagr = revenue_cagr(revenue_points, 3) if len(revenue_points) >= 2 else None

    gross_margin_latest = next(
        (p.gross_margin for p in reversed(annual) if p.gross_margin is not None), None
    )

    quarterly_fcf = [p.free_cash_flow for p in quarterly if p.free_cash_flow is not None]
    burn_rate: float | None = None
    runway_months: float | None = None
    if quarterly_fcf:
        avg_fcf = sum(quarterly_fcf) / len(quarterly_fcf)
        if avg_fcf < 0:
            burn_rate = -avg_fcf
            latest_cash = next(
                (p.cash_and_equivalents for p in reversed(quarterly) if p.cash_and_equivalents is not None),
                None,
            )
            if latest_cash is None:
                latest_cash = next(
                    (p.cash_and_equivalents for p in reversed(annual) if p.cash_and_equivalents is not None),
                    None,
                )
            if latest_cash is not None and latest_cash > 0:
                monthly_burn = burn_rate / _MONTHS_PER_QUARTER
                runway_months = latest_cash / monthly_burn

    share_points = [
        (p.period_end, p.shares_outstanding)
        for p in annual
        if p.shares_outstanding is not None and p.shares_outstanding > 0
    ]
    share_growth, _window = dilution_cagr_with_window(share_points)

    labels = dict(PIOTROSKI_CRITERIA)
    criteria = [
        PiotroskiCriterion(key=key, label=labels[key], met=piotroski.criteria.get(key))
        for key, _label in PIOTROSKI_CRITERIA
    ]

    return FinancialHistoryDerived(
        revenue_yoy=yoy,
        revenue_cagr_3y=cagr,
        gross_margin_latest=gross_margin_latest,
        quarterly_burn_rate=burn_rate,
        runway_months=runway_months,
        runway_floor_months=runway_floor_months,
        share_growth_rate=share_growth,
        piotroski_score_ratio=piotroski.score_ratio,
        piotroski_criteria_met=piotroski.criteria_met,
        piotroski_criteria_computable=piotroski.criteria_computable,
        piotroski_criteria=criteria,
    )


def build_financial_history(
    payload: dict, runway_floor_months: float | None = None
) -> FinancialHistory:
    """`raw_snapshots.payload` から財務推移ビューを組み立てる。

    `payload` に財務三表が1行も無い場合でも例外にはせず、空の系列と
    None 埋めの `derived` を返す(詳細画面をエラーにしない。J-2 受け入れ基準)。
    """
    info = payload.get("info") or {}
    fx = financial_to_trading_rate(payload)
    conversion_unavailable = fx is None
    if fx is None:
        fx = 1.0

    # 可視期間を全期に開く(表示では先読みバイアスの心配がない)。
    statements = build_point_in_time_statements(payload, datetime.date.max)
    annual_ends = statements.visible_period_ends or _all_period_ends(
        statements.income_stmt, statements.balance_sheet, statements.cash_flow
    )
    annual = _build_periods(
        annual_ends,
        statements.income_stmt,
        statements.balance_sheet,
        statements.cash_flow,
        fx,
        _MAX_ANNUAL,
    )

    q_income = payload.get("quarterly_income_stmt") or {}
    q_balance = payload.get("quarterly_balance_sheet") or {}
    q_cash = payload.get("quarterly_cash_flow") or {}
    quarterly_ends = _all_period_ends(q_income, q_balance, q_cash)
    quarterly = _build_periods(quarterly_ends, q_income, q_balance, q_cash, fx, _MAX_QUARTERLY)

    piotroski = piotroski_f_score(
        statements.balance_sheet, statements.income_stmt, statements.cash_flow
    )
    derived = _derived(annual, quarterly, piotroski, runway_floor_months)

    as_of = annual[-1].period_end if annual else (quarterly[-1].period_end if quarterly else None)
    return FinancialHistory(
        currency=info.get("currency") or info.get("financialCurrency"),
        currency_conversion_unavailable=conversion_unavailable,
        annual=annual,
        quarterly=quarterly,
        derived=derived,
        as_of=as_of,
    )
