import pytest

from autoscreener.screening.earnings_history import build_earnings_history


def test_future_period_is_not_mixed_into_actuals():
    history = build_earnings_history({"earnings_dates": [
        {"date": "2026-02-01", "eps_estimate": 1.0, "reported_eps": None},
        {"date": "2025-11-01", "eps_estimate": 1.0, "reported_eps": 1.2},
    ]})
    assert len(history.periods) == 1
    assert history.next_estimate == 1.0
    assert history.periods[0].surprise_pct == pytest.approx(0.2)


def test_zero_or_negative_estimate_has_no_surprise_percent():
    history = build_earnings_history({"earnings_dates": [
        {"date": "2025-11-01", "eps_estimate": 0, "reported_eps": 1},
        {"date": "2025-08-01", "eps_estimate": -1, "reported_eps": -0.5},
    ]})
    assert all(p.surprise_pct is None for p in history.periods)
