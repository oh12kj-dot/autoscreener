"""tests/unit/test_liquidity.py(30.2.4)。"""

from __future__ import annotations

from autoscreener.screening.liquidity import (
    LIQUIDITY_BINDING,
    PORTFOLIO_BINDING,
    compute_liquidity_profile,
)


def test_adv_is_simple_mean_of_dollar_volume():
    rows = [(10.0, 1000)] * 20  # 20日 x $10,000/日
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.adv_usd == 10_000.0
    assert profile.observation_days == 20


def test_fewer_than_min_observation_days_returns_none():
    rows = [(10.0, 1000)] * 4
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.adv_usd is None
    assert profile.observation_days == 4


def test_between_min_and_window_still_computes_but_reports_observation_days():
    rows = [(10.0, 1000)] * 10
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.adv_usd == 10_000.0
    assert profile.observation_days == 10


def test_none_rows_are_excluded_from_denominator():
    rows = [(10.0, 1000)] * 5 + [(None, None)] * 3
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.observation_days == 5
    assert profile.adv_usd == 10_000.0


def test_liquidity_binding_when_adv_cap_smaller():
    rows = [(1.0, 1000)] * 20  # ADV = $1,000 -> cap 10% = $100
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.max_position_adv_usd == 100.0
    assert profile.max_position_portfolio_usd == 4_000.0
    assert profile.max_position_usd == 100.0
    assert profile.binding_constraint == LIQUIDITY_BINDING


def test_portfolio_binding_when_portfolio_cap_smaller():
    rows = [(1000.0, 100_000)] * 20  # ADV = $100,000,000 -> cap 10% = $10,000,000
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.max_position_usd == 4_000.0
    assert profile.binding_constraint == PORTFOLIO_BINDING


def test_no_price_data_returns_none_without_error():
    profile = compute_liquidity_profile(
        [], portfolio_value_usd=100_000.0, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.adv_usd is None
    assert profile.max_position_usd == 4_000.0  # ポートフォリオ制約だけは計算できる
    assert profile.binding_constraint == PORTFOLIO_BINDING


def test_no_portfolio_value_returns_none_for_portfolio_cap():
    rows = [(10.0, 1000)] * 20
    profile = compute_liquidity_profile(
        rows, portfolio_value_usd=None, adv_participation_cap=0.10, per_position_cap=0.04
    )
    assert profile.max_position_portfolio_usd is None
    assert profile.max_position_usd == profile.max_position_adv_usd
    assert profile.binding_constraint == LIQUIDITY_BINDING
