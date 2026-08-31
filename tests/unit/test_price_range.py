"""J-3(docs/investment_decision_gap_2026-08-29.md):52週レンジのテスト。"""

from __future__ import annotations

from autoscreener.screening.price_range import compute_price_range


def test_position_in_range_between_low_and_high() -> None:
    result = compute_price_range([10.0, 20.0, 30.0, 15.0])
    assert result is not None
    assert result.week52_high == 30.0
    assert result.week52_low == 10.0
    # current = 15 → (15-10)/(30-10) = 0.25
    assert result.position_in_range == 0.25


def test_no_price_movement_does_not_divide_by_zero() -> None:
    result = compute_price_range([25.0, 25.0, 25.0])
    assert result is not None
    assert result.week52_high == result.week52_low == 25.0
    assert result.position_in_range is None


def test_returns_none_when_no_positive_closes() -> None:
    assert compute_price_range([]) is None
    assert compute_price_range([0.0, -1.0, None]) is None


def test_position_clamped_to_unit_interval() -> None:
    result = compute_price_range([100.0, 50.0, 200.0, 200.0])
    assert result is not None
    assert 0.0 <= result.position_in_range <= 1.0
    assert result.position_in_range == 1.0
