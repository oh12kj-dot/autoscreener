"""ポートフォリオ水準の確率のテスト(28.12)。"""

import pytest

from autoscreener.scoring.portfolio import (
    conditional_hit_probability,
    estimate_asset_correlation,
    portfolio_outcome,
)


def test_zero_correlation_reproduces_the_independent_formula():
    """ρ=0 なら教科書どおり 1 − Π(1−p_i) に一致すること。"""
    probabilities = [0.05, 0.03, 0.02, 0.01]
    outcome = portfolio_outcome(probabilities, rho=0.0)
    expected = 1 - (0.95 * 0.97 * 0.98 * 0.99)
    assert outcome.probability_at_least_one == pytest.approx(expected, rel=1e-3)
    assert outcome.probability_at_least_one_if_independent == pytest.approx(expected, rel=1e-9)


def test_correlation_lowers_the_probability_of_at_least_one():
    """**これが伝えたい事実そのもの**(28.12)。

    共通因子が正に効くと結果は固まる——当たる年は多くの銘柄が当たり、外れる年は
    どれも外れる。同じ期待本数でも「少なくとも1つ」の確率は独立の場合より低い。
    20銘柄持っても20回の独立な試行にはならない。
    """
    probabilities = [0.03] * 30
    independent = portfolio_outcome(probabilities, rho=0.0)
    correlated = portfolio_outcome(probabilities, rho=0.30)
    assert correlated.probability_at_least_one < independent.probability_at_least_one
    # 期待本数は相関に依存しない(線形性)
    assert correlated.expected_hits == pytest.approx(independent.expected_hits)


def test_expected_hits_is_the_sum_of_probabilities():
    outcome = portfolio_outcome([0.05, 0.03, 0.02], rho=0.25)
    assert outcome.expected_hits == pytest.approx(0.10)


def test_at_least_two_never_exceeds_at_least_one():
    outcome = portfolio_outcome([0.04] * 25, rho=0.15)
    assert 0.0 <= outcome.probability_at_least_two <= outcome.probability_at_least_one <= 1.0


def test_conditional_probability_moves_with_the_common_factor():
    """共通因子が良い年は全銘柄の当たり確率が上がり、悪い年は下がる。"""
    good = conditional_hit_probability(0.05, factor=2.0, rho=0.30)
    neutral = conditional_hit_probability(0.05, factor=0.0, rho=0.30)
    bad = conditional_hit_probability(0.05, factor=-2.0, rho=0.30)
    assert good > neutral > bad


def test_empty_portfolio_is_handled():
    outcome = portfolio_outcome([], rho=0.2)
    assert outcome.holdings == 0
    assert outcome.probability_at_least_one == 0.0


# --- 相関の推定 ---------------------------------------------------------------


def test_correlation_estimate_is_zero_when_dates_agree():
    """評価日ごとの的中率が揃っているなら、共通因子は効いていない。"""
    rates = [0.25, 0.25, 0.25, 0.25, 0.25]
    counts = [500] * 5
    assert estimate_asset_correlation(rates, counts) == pytest.approx(0.0, abs=1e-6)


def test_correlation_estimate_rises_with_dispersion():
    """的中率が年ごとに大きく振れるほど、共通因子の効きは強いと推定される。"""
    counts = [500] * 6
    mild = estimate_asset_correlation([0.24, 0.25, 0.26, 0.25, 0.24, 0.26], counts)
    wild = estimate_asset_correlation([0.10, 0.45, 0.15, 0.40, 0.12, 0.38], counts)
    assert mild is not None and wild is not None
    assert wild > mild


def test_correlation_estimate_needs_enough_dates():
    assert estimate_asset_correlation([0.2, 0.3], [500, 500]) is None


def test_binomial_noise_alone_does_not_create_correlation():
    """**二項ノイズの分は差し引く**(28.12)。

    観測数が少ない評価日では的中率がノイズだけで振れる。それを共通因子の効きと
    見なすと、銘柄数が少ないほど相関が高く推定されるという逆立ちが起きる。
    """
    # n=50 で p=0.25 なら標準偏差は約6.1pt。その程度の散らばりは相関ではない。
    rates = [0.19, 0.31, 0.25, 0.20, 0.30, 0.25]
    small = estimate_asset_correlation(rates, [50] * 6)
    large = estimate_asset_correlation(rates, [5000] * 6)
    assert small is not None and large is not None
    assert small < large
