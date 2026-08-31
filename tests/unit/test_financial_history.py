"""J-2(docs/investment_decision_gap_2026-08-29.md):財務推移ビューのテスト。

`build_financial_history` は表示専用の純関数(DB不要)。
"""

from __future__ import annotations

import datetime

from autoscreener.scoring.financial_metrics import piotroski_f_score
from autoscreener.scoring.point_in_time import build_point_in_time_statements
from autoscreener.screening.financial_history import build_financial_history


def _base_payload() -> dict:
    return {
        "info": {"currency": "USD", "financialCurrency": "USD"},
        "income_stmt": {
            "Total Revenue": {
                "2021-12-31": 100.0,
                "2022-12-31": 150.0,
                "2023-12-31": 210.0,
                "2024-12-31": 300.0,
            },
            "Gross Profit": {
                "2021-12-31": 40.0,
                "2022-12-31": 66.0,
                "2023-12-31": 100.0,
                "2024-12-31": 150.0,
            },
            "Operating Income": {"2023-12-31": -30.0, "2024-12-31": -12.0},
            "Net Income": {"2023-12-31": -20.0, "2024-12-31": -10.0},
        },
        "balance_sheet": {
            "Total Assets": {"2023-12-31": 500.0, "2024-12-31": 520.0},
            "Total Debt": {"2023-12-31": 50.0, "2024-12-31": 40.0},
            "Current Assets": {"2023-12-31": 200.0, "2024-12-31": 260.0},
            "Current Liabilities": {"2023-12-31": 150.0, "2024-12-31": 120.0},
            "Cash And Cash Equivalents": {"2023-12-31": 120.0, "2024-12-31": 90.0},
            "Ordinary Shares Number": {
                "2021-12-31": 10.0,
                "2022-12-31": 11.0,
                "2023-12-31": 12.0,
                "2024-12-31": 13.0,
            },
        },
        "cash_flow": {
            "Free Cash Flow": {"2023-12-31": -40.0, "2024-12-31": -30.0},
            "Operating Cash Flow": {"2023-12-31": -20.0, "2024-12-31": -10.0},
            "Capital Expenditure": {"2023-12-31": -20.0, "2024-12-31": -20.0},
        },
        "quarterly_income_stmt": {
            "Total Revenue": {
                "2024-03-31": 60.0,
                "2024-06-30": 70.0,
                "2024-09-30": 80.0,
                "2024-12-31": 90.0,
                "2025-03-31": 100.0,
            }
        },
        "quarterly_cash_flow": {
            "Free Cash Flow": {
                "2024-03-31": -8.0,
                "2024-06-30": -9.0,
                "2024-09-30": -7.0,
                "2024-12-31": -6.0,
                "2025-03-31": -5.0,
            }
        },
        "quarterly_balance_sheet": {
            "Cash And Cash Equivalents": {"2024-12-31": 90.0, "2025-03-31": 84.0}
        },
    }


def test_builds_annual_and_quarterly_series() -> None:
    history = build_financial_history(_base_payload(), runway_floor_months=12)

    assert [p.period_end for p in history.annual] == [
        datetime.date(2021, 12, 31),
        datetime.date(2022, 12, 31),
        datetime.date(2023, 12, 31),
        datetime.date(2024, 12, 31),
    ]
    assert [p.revenue for p in history.annual] == [100.0, 150.0, 210.0, 300.0]
    assert history.annual[-1].gross_margin == 0.5
    # net_debt = total_debt - cash = 40 - 90
    assert history.annual[-1].net_debt == -50.0
    assert len(history.quarterly) == 5
    assert history.quarterly[-1].period_end == datetime.date(2025, 3, 31)
    assert history.as_of == datetime.date(2024, 12, 31)
    assert history.derived.revenue_yoy == 300.0 / 210.0 - 1
    assert history.derived.runway_floor_months == 12


def test_converts_when_financial_currency_differs_from_trading_currency() -> None:
    payload = _base_payload()
    payload["info"] = {
        "currency": "USD",
        "financialCurrency": "EUR",
        "_fx_rate_financial_to_trading": 1.1,
    }
    history = build_financial_history(payload)

    assert history.currency_conversion_unavailable is False
    assert history.annual[-1].revenue == 300.0 * 1.1
    assert history.annual[-1].cash_and_equivalents == 90.0 * 1.1
    # 比(粗利率)は通貨換算しても不変
    assert abs(history.annual[-1].gross_margin - 0.5) < 1e-12


def test_missing_fx_rate_flags_conversion_unavailable_without_raising() -> None:
    payload = _base_payload()
    payload["info"] = {"currency": "USD", "financialCurrency": "EUR"}  # レートなし
    history = build_financial_history(payload)

    assert history.currency_conversion_unavailable is True
    # 換算せず決算通貨のまま返す
    assert history.annual[-1].revenue == 300.0


def test_missing_gross_profit_row_yields_none_not_exception() -> None:
    payload = _base_payload()
    del payload["income_stmt"]["Gross Profit"]
    history = build_financial_history(payload)

    assert all(p.gross_profit is None for p in history.annual)
    assert all(p.gross_margin is None for p in history.annual)
    assert history.derived.gross_margin_latest is None


def test_empty_payload_returns_empty_history() -> None:
    history = build_financial_history({})
    assert history.annual == []
    assert history.quarterly == []
    assert history.as_of is None
    assert history.derived.runway_months is None


def test_positive_fcf_company_has_no_finite_runway() -> None:
    payload = _base_payload()
    payload["quarterly_cash_flow"]["Free Cash Flow"] = {
        "2024-03-31": 5.0,
        "2024-06-30": 6.0,
        "2024-09-30": 7.0,
        "2024-12-31": 8.0,
    }
    history = build_financial_history(payload)

    assert history.derived.quarterly_burn_rate is None
    assert history.derived.runway_months is None


def test_piotroski_breakdown_matches_scalar_score() -> None:
    payload = _base_payload()
    statements = build_point_in_time_statements(payload, datetime.date.max)
    reference = piotroski_f_score(
        statements.balance_sheet, statements.income_stmt, statements.cash_flow
    )

    history = build_financial_history(payload)
    d = history.derived
    assert d.piotroski_score_ratio == reference.score_ratio
    assert d.piotroski_criteria_met == reference.criteria_met
    assert d.piotroski_criteria_computable == reference.criteria_computable

    met = sum(1 for c in d.piotroski_criteria if c.met is True)
    computable = sum(1 for c in d.piotroski_criteria if c.met is not None)
    assert met == reference.criteria_met
    assert computable == reference.criteria_computable
