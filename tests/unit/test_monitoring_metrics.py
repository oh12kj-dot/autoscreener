"""tests/unit/test_monitoring_metrics.py(30.7.6)。"""

from __future__ import annotations

import datetime

import pytest

from autoscreener.screening.monitoring_metrics import (
    CASH_RUNWAY_LOW,
    CUSTOMER_CONCENTRATION_DISCLOSED_DROP,
    GROSS_MARGIN_DECLINE,
    REVENUE_GROWTH_DECELERATION,
    SHARE_COUNT_GROWTH,
    MonitoringThresholds,
    evaluate_customer_concentration_metric,
    evaluate_monitoring,
)

_THRESHOLDS = MonitoringThresholds()


def _quarterly(dates_values: list[tuple[str, float]]) -> dict[str, float]:
    return dict(dates_values)


def test_gross_margin_decline_triggers_on_two_consecutive_drops():
    income_stmt = {
        "Total Revenue": _quarterly(
            [("2026-06-30", 100.0), ("2025-06-30", 100.0), ("2024-06-30", 100.0), ("2023-06-30", 100.0)]
        ),
        "Gross Profit": _quarterly(
            [("2026-06-30", 30.0), ("2025-06-30", 40.0), ("2024-06-30", 50.0), ("2023-06-30", 50.0)]
        ),
    }
    metrics = evaluate_monitoring(income_stmt, {}, None, [], _THRESHOLDS)
    margin_metric = next(m for m in metrics if m.code == GROSS_MARGIN_DECLINE)
    assert margin_metric.triggered is True


def test_gross_margin_not_declining_does_not_trigger():
    income_stmt = {
        "Total Revenue": _quarterly([("2026-06-30", 100.0), ("2025-06-30", 100.0)]),
        "Gross Profit": _quarterly([("2026-06-30", 50.0), ("2025-06-30", 40.0)]),
    }
    metrics = evaluate_monitoring(income_stmt, {}, None, [], _THRESHOLDS)
    margin_metric = next(m for m in metrics if m.code == GROSS_MARGIN_DECLINE)
    assert margin_metric.triggered is False


def test_share_count_growth_above_ceiling_triggers():
    share_counts = [
        (datetime.date(2025, 8, 1), 100_000_000),
        (datetime.date(2026, 8, 1), 130_000_000),  # +30%/年
    ]
    metrics = evaluate_monitoring({}, {}, None, share_counts, _THRESHOLDS)
    m = next(x for x in metrics if x.code == SHARE_COUNT_GROWTH)
    assert m.triggered is True
    assert m.current_value == pytest.approx(0.30, rel=0.05)


def test_share_count_growth_below_ceiling_does_not_trigger():
    share_counts = [
        (datetime.date(2025, 8, 1), 100_000_000),
        (datetime.date(2026, 8, 1), 105_000_000),  # +5%/年
    ]
    metrics = evaluate_monitoring({}, {}, None, share_counts, _THRESHOLDS)
    m = next(x for x in metrics if x.code == SHARE_COUNT_GROWTH)
    assert m.triggered is False


def test_share_count_insufficient_history_does_not_trigger():
    metrics = evaluate_monitoring({}, {}, None, [], _THRESHOLDS)
    m = next(x for x in metrics if x.code == SHARE_COUNT_GROWTH)
    assert m.triggered is False
    assert m.current_value is None


def test_cash_runway_below_floor_triggers():
    quarterly_cash_flow = {
        "Free Cash Flow": _quarterly(
            [("2026-06-30", -10.0), ("2025-03-31", -10.0), ("2024-12-31", -10.0), ("2024-09-30", -10.0)]
        )
    }
    metrics = evaluate_monitoring({}, quarterly_cash_flow, 20.0, [], _THRESHOLDS)  # 20/10=2四半期=6か月
    m = next(x for x in metrics if x.code == CASH_RUNWAY_LOW)
    assert m.triggered is True


def test_cash_runway_positive_fcf_never_triggers():
    quarterly_cash_flow = {"Free Cash Flow": _quarterly([("2026-06-30", 10.0)])}
    metrics = evaluate_monitoring({}, quarterly_cash_flow, 20.0, [], _THRESHOLDS)
    m = next(x for x in metrics if x.code == CASH_RUNWAY_LOW)
    assert m.triggered is False


def test_missing_data_does_not_raise():
    metrics = evaluate_monitoring({}, {}, None, [], _THRESHOLDS)
    assert len(metrics) == 4
    for m in metrics:
        assert m.triggered is False


# --- K-3:customer_concentration_disclosed_drop ---------------------------------
#
# `evaluate_monitoring()` の引数・戻り値件数(4件)はあえて変更していない
# (既存呼び出し・上のテストを壊さないため)。新指標は独立関数として直接テストする。


def test_customer_concentration_drop_disclosure_disappeared_triggers():
    history = [(datetime.date(2024, 12, 31), 0.23), (datetime.date(2025, 12, 31), None)]
    metric = evaluate_customer_concentration_metric(history, _THRESHOLDS)
    assert metric.code == CUSTOMER_CONCENTRATION_DISCLOSED_DROP
    assert metric.triggered is True


def test_customer_concentration_pct_drop_beyond_threshold_triggers():
    history = [(datetime.date(2024, 12, 31), 0.30), (datetime.date(2025, 12, 31), 0.20)]
    metric = evaluate_customer_concentration_metric(history, _THRESHOLDS)
    assert metric.triggered is True
    assert metric.current_value == pytest.approx(0.20)
    assert metric.previous_value == pytest.approx(0.30)


def test_customer_concentration_stable_disclosure_does_not_trigger():
    history = [(datetime.date(2024, 12, 31), 0.23), (datetime.date(2025, 12, 31), 0.24)]
    metric = evaluate_customer_concentration_metric(history, _THRESHOLDS)
    assert metric.triggered is False


def test_customer_concentration_insufficient_history_does_not_trigger():
    metric = evaluate_customer_concentration_metric([], _THRESHOLDS)
    assert metric.triggered is False
    assert metric.current_value is None


def test_revenue_growth_deceleration_uses_yoy_comparison():
    # 8四半期分:直近2件のYoY成長率が減速している(80%→50%→20%)ケース
    revenue = _quarterly(
        [
            (f"{y}-{q}", v)
            for y, q, v in [
                ("2023", "03-31", 100),
                ("2023", "06-30", 100),
                ("2023", "09-30", 100),
                ("2023", "12-31", 100),
                ("2024", "03-31", 180),  # YoY +80%
                ("2024", "06-30", 150),  # YoY +50%
                ("2024", "09-30", 120),  # YoY +20%
            ]
        ]
    )
    metrics = evaluate_monitoring({"Total Revenue": revenue}, {}, None, [], _THRESHOLDS)
    m = next(x for x in metrics if x.code == REVENUE_GROWTH_DECELERATION)
    assert m.triggered is True
