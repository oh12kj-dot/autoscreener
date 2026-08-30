import datetime as dt

from autoscreener.scoring.portfolio import pairwise_return_correlation


def test_identical_price_series_have_one_correlation():
    series = [(dt.date(2025, 1, 1) + dt.timedelta(days=i), 100 + i) for i in range(121)]
    result = pairwise_return_correlation({"A": series, "B": series}, min_overlap=120)
    assert result[("A", "B")][0] == 1.0
