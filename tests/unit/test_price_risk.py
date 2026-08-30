import datetime as dt

from autoscreener.screening.price_risk import compute_price_risk


def series(values):
    start = dt.date(2025, 1, 1)
    return [(start + dt.timedelta(days=i), value) for i, value in enumerate(values)]


def test_v_shape_drawdown_and_recovery():
    risk = compute_price_risk(series([100, 50, 100] * 25), None, min_observations=3)
    assert risk is not None
    assert risk.max_drawdown_3y == -0.5
    assert risk.recovery_days_3y == 1


def test_short_series_is_not_fake_zero():
    risk = compute_price_risk(series([100 + i for i in range(59)]), None)
    assert risk is not None
    assert risk.realized_vol_1y is None


def test_benchmark_missing_keeps_own_statistics():
    risk = compute_price_risk(series([100 + i for i in range(70)]), None)
    assert risk is not None
    assert risk.beta_1y is None
    assert risk.max_drawdown_3y == 0
