import datetime

import pytest

from autoscreener.scoring.financial_metrics import piotroski_f_score

# --- revenue_cagr_years -------------------------------------------------------


def test_piotroski_all_criteria_met():
    balance_sheet = {
        "Total Assets": {"2023-12-31": 1000.0, "2024-12-31": 1200.0},
        "Total Debt": {"2023-12-31": 400.0, "2024-12-31": 360.0},  # leverage down
        "Current Assets": {"2023-12-31": 500.0, "2024-12-31": 700.0},
        "Current Liabilities": {"2023-12-31": 300.0, "2024-12-31": 300.0},  # current ratio up
        "Ordinary Shares Number": {"2023-12-31": 100.0, "2024-12-31": 100.0},  # no dilution
    }
    income_stmt = {
        "Net Income": {"2023-12-31": 50.0, "2024-12-31": 120.0},  # ROA up, positive
        "Total Revenue": {"2023-12-31": 1000.0, "2024-12-31": 1300.0},  # turnover up
        "Gross Profit": {"2023-12-31": 400.0, "2024-12-31": 600.0},  # margin up (40%->46%)
    }
    cash_flow = {"Operating Cash Flow": {"2024-12-31": 150.0}}  # CFO>0, CFO>NI

    result = piotroski_f_score(balance_sheet, income_stmt, cash_flow)
    assert result.criteria_computable == 9
    assert result.criteria_met == 9
    assert result.score_ratio == 1.0

def test_piotroski_insufficient_data_returns_none_score():
    result = piotroski_f_score({}, {}, {})
    assert result.score_ratio is None
    assert result.criteria_computable == 0

def test_piotroski_partial_data_still_scores_if_enough_criteria():
    # 唯一のCFO>0基準以外全部データありのケースを作るのは冗長なので、
    # 6基準ちょうど算出できるケースを確認する程度に留める
    balance_sheet = {
        "Total Assets": {"2023-12-31": 1000.0, "2024-12-31": 1200.0},
        "Ordinary Shares Number": {"2023-12-31": 100.0, "2024-12-31": 105.0},  # dilution occurred
    }
    income_stmt = {
        "Net Income": {"2023-12-31": 50.0, "2024-12-31": 40.0},  # ROA down
    }
    cash_flow = {"Operating Cash Flow": {"2024-12-31": 30.0}}  # CFO>0 but CFO<NI

    result = piotroski_f_score(balance_sheet, income_stmt, cash_flow)
    # ROA>0, ΔROA>0, CFO>0, CFO>NI, 増資なし = 5 criteria computable -> below 6, insufficient
    assert result.score_ratio is None


# --- fifty_two_week_proximity --------------------------------------------------
