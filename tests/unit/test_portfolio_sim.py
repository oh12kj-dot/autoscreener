"""ポートフォリオ・シミュレーションのテスト(docs/defect_and_edge_audit_2026-08-28.md D-4)。純粋関数。"""

import pytest

from autoscreener.backtest.portfolio_sim import SimObservation, simulate_portfolio


def _obs(base_date: str, n: int, base_return: float, sector: str | None = None):
    return [
        SimObservation(
            ticker_id=hash((base_date, i)) % 100000,
            base_date=base_date,
            probability=(n - i) / n,
            realized_return=base_return + i * 0.001,
            sector=sector,
            cost_bps=50.0,
        )
        for i in range(n)
    ]


def test_returns_none_without_observations():
    assert simulate_portfolio([], horizon_years=1.0, max_positions=30, per_position_cap=0.04, sector_cap=0.25) is None


def test_basic_cagr_from_non_overlapping_tranches():
    # 3つの評価日、間隔1年 -> 3トランシェ。各バスケット +20%/年。
    obs = _obs("2023-01-02", 40, 0.20) + _obs("2024-01-02", 40, 0.20) + _obs("2025-01-02", 40, 0.20)
    result = simulate_portfolio(
        obs, horizon_years=1.0, max_positions=30, per_position_cap=0.04, sector_cap=0.25
    )
    assert result is not None
    assert result.non_overlapping_tranche_count == 3
    assert result.holdings_per_rebalance == 30
    # 各期 ~+20% (i の傾斜で少し上) を3年複利 -> CAGR ~ +20%
    assert 0.18 < result.cagr < 0.24
    assert result.max_drawdown == pytest.approx(0.0)  # 3期とも正


def test_excess_cagr_vs_benchmark():
    obs = _obs("2023-01-02", 40, 0.30) + _obs("2024-01-02", 40, 0.30) + _obs("2025-01-02", 40, 0.30)
    bench = {
        "2023-01-02": {"IWC": 0.10},
        "2024-01-02": {"IWC": 0.10},
        "2025-01-02": {"IWC": 0.10},
    }
    result = simulate_portfolio(
        obs,
        horizon_years=1.0,
        max_positions=30,
        per_position_cap=0.04,
        sector_cap=0.25,
        benchmark_returns=bench,
    )
    assert result.benchmark_cagr["IWC"] == pytest.approx(0.10, abs=1e-9)
    assert result.excess_cagr["IWC"] == pytest.approx(result.cagr - 0.10, abs=1e-9)
    assert result.win_rate_vs_benchmark["IWC"] == pytest.approx(1.0)  # 毎期勝つ


def test_sector_cap_limits_concentration():
    # 全銘柄同一セクター、sector_cap=0.25、max_positions=30 -> 最大 7 銘柄。
    obs = _obs("2024-01-02", 40, 0.10, sector="Tech")
    result = simulate_portfolio(
        obs, horizon_years=1.0, max_positions=30, per_position_cap=0.04, sector_cap=0.25
    )
    assert result.holdings_per_rebalance == 7  # floor(30 * 0.25)


def test_net_of_cost_produces_positive_cost_drag():
    obs = _obs("2023-01-02", 40, 0.20) + _obs("2024-01-02", 40, 0.20) + _obs("2025-01-02", 40, 0.20)
    result = simulate_portfolio(
        obs,
        horizon_years=1.0,
        max_positions=30,
        per_position_cap=0.04,
        sector_cap=0.25,
        net_of_cost=True,
    )
    # cost_bps=50(=0.5%)を毎期引くので、コスト差引 ≈ 0.5%/年
    assert result.realized_cost_drag == pytest.approx(0.005, abs=1.5e-3)
    # グロスは ~+21.5%(iの傾斜込み)。コスト後はそれより低い。
    assert result.cagr < 0.215


def test_max_drawdown_is_negative_when_a_tranche_loses():
    obs = _obs("2023-01-02", 40, 0.20) + _obs("2024-01-02", 40, -0.40) + _obs("2025-01-02", 40, 0.20)
    result = simulate_portfolio(
        obs, horizon_years=1.0, max_positions=30, per_position_cap=0.04, sector_cap=0.25
    )
    assert result.max_drawdown < -0.30
