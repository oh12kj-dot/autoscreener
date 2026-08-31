"""取引コスト推定のテスト(docs/defect_and_edge_audit_2026-08-28.md D-5 / I-7)。純粋関数のみ。"""

import math

import pytest

from autoscreener.screening.trading_cost import (
    amihud_illiquidity,
    corwin_schultz_spread,
    round_trip_cost_bps,
)


def test_corwin_schultz_zero_range_gives_zero_spread():
    """高値=安値(値動きなし)の日が続けばスプレッド推定は 0。"""
    bars = [(10.0, 10.0)] * 10
    assert corwin_schultz_spread(bars) == pytest.approx(0.0, abs=1e-9)


def test_corwin_schultz_positive_for_realistic_microcap_range():
    """1〜3% の高値安値レンジがある薄商い銘柄では、正の実効スプレッドが出る。"""
    bars = [(10.2, 9.9), (10.3, 10.0), (10.1, 9.85), (10.25, 9.95)] * 6
    spread = corwin_schultz_spread(bars)
    assert spread is not None
    assert 0.0 < spread < 0.10  # 数%オーダー


def test_corwin_schultz_needs_two_bars():
    assert corwin_schultz_spread([(10.0, 9.5)]) is None
    assert corwin_schultz_spread([]) is None


def test_corwin_schultz_respects_window():
    """window より前のバーは無視される。"""
    calm = [(10.0, 10.0)] * 30
    spike = [(12.0, 8.0), (12.0, 8.0)]
    # 直近2本だけがワイドレンジ。window=2 なら効くが window=1 なら…最低1ペアは要る
    wide = corwin_schultz_spread(calm + spike, window=2)
    narrow = corwin_schultz_spread(calm, window=2)
    assert wide is not None and narrow is not None
    assert wide > narrow


def test_amihud_higher_when_price_moves_on_small_volume():
    illiquid = amihud_illiquidity([0.05, -0.04, 0.06], [1_000, 1_200, 900])
    liquid = amihud_illiquidity([0.005, -0.004, 0.006], [10_000_000, 12_000_000, 9_000_000])
    assert illiquid is not None and liquid is not None
    assert illiquid > liquid * 100


def test_amihud_none_without_usable_pairs():
    assert amihud_illiquidity([], []) is None
    assert amihud_illiquidity([0.01, 0.02], [0.0, None]) is None


def test_round_trip_cost_spread_component_is_full_spread():
    """インパクト・手数料ゼロなら、往復コストはほぼ1スプレッドぶん(bps)。"""
    cost = round_trip_cost_bps(spread=0.02, position_usd=0.0, adv_usd=None, impact_coefficient=0.1)
    assert cost == pytest.approx(200.0)  # 2% -> 200bps


def test_round_trip_cost_impact_scales_with_sqrt_participation():
    small = round_trip_cost_bps(0.0, position_usd=10_000, adv_usd=1_000_000, impact_coefficient=0.1)
    big = round_trip_cost_bps(0.0, position_usd=40_000, adv_usd=1_000_000, impact_coefficient=0.1)
    # 4倍の建玉 -> sqrt則で 2倍のインパクト
    assert big == pytest.approx(2 * small)


def test_round_trip_cost_uses_min_half_spread_floor_when_spread_missing():
    cost = round_trip_cost_bps(
        spread=None, position_usd=0.0, adv_usd=None, impact_coefficient=0.1, min_half_spread_bps=15.0
    )
    assert cost == pytest.approx(30.0)


def test_round_trip_cost_floor_applies_even_with_tiny_spread():
    cost = round_trip_cost_bps(
        spread=0.0001, position_usd=0.0, adv_usd=None, impact_coefficient=0.1, min_half_spread_bps=15.0
    )
    assert cost == pytest.approx(30.0)
