"""ベースライン比較(D-8)と層別KPI(C-5 / I-4)のテスト。純粋関数。"""

from __future__ import annotations

from autoscreener.backtest.baselines import BASELINES, baseline_metrics
from autoscreener.backtest.metrics import Observation
from autoscreener.backtest.stratify import stratify_kpis


def _obs(base_date: str, i: int, n: int, *, mom: float, ret: float, mcap: float) -> Observation:
    return Observation(
        ticker_id=i,
        base_date=base_date,
        probability=(n - i) / n,
        log_moic_mu=0.5,
        log_moic_sigma=1.0,
        realized_return=ret,
        settlement="market",
        market_cap=mcap,
        baseline_scores={
            "momentum_12m": mom,
            "revenue_growth": mom * 0.5,
            "cheapness": -mom,
            "gross_profit_scale": mcap,
            "random": (i * 37 % n) / n,
        },
    )


def test_baseline_metrics_reports_every_baseline():
    obs = []
    for d in ("2024-01-01", "2024-04-01", "2024-07-01"):
        for i in range(120):
            # モメンタムが高いほどリターンが高い -> momentum_12m は強い順位ICになるはず
            obs.append(_obs(d, i, 120, mom=(120 - i) / 120, ret=((120 - i) / 120) - 0.3, mcap=1e8 + i * 1e6))
    result = baseline_metrics(obs, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert set(result) <= set(BASELINES)
    assert "momentum_12m" in result
    assert result["momentum_12m"]["rank_ic"] > 0.3  # 構成上、強く正


def test_baseline_skips_when_all_scores_none():
    obs = [
        Observation(
            ticker_id=i,
            base_date="2024-01-01",
            probability=0.1,
            log_moic_mu=0.0,
            log_moic_sigma=1.0,
            realized_return=0.0,
            settlement="market",
            baseline_scores={"momentum_12m": None},
        )
        for i in range(50)
    ]
    result = baseline_metrics(obs, 10.0, 1.0, 7.0)
    assert "momentum_12m" not in result


def test_stratify_kpis_splits_into_five_buckets():
    obs = []
    for d in ("2024-01-01", "2024-04-01", "2024-07-01"):
        for i in range(400):
            obs.append(_obs(d, i, 400, mom=0.0, ret=((400 - i) / 400) - 0.5, mcap=1e7 * (i + 1)))
    result = stratify_kpis(
        obs, key=lambda o: o.market_cap, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0
    )
    assert set(result) == {f"bucket_{i}" for i in range(5)}
    # 各バケットに観測がある
    assert all(result[f"bucket_{i}"].get("n", 0) > 0 for i in range(5))


def test_stratify_reports_insufficient_when_too_small():
    obs = [
        Observation(
            ticker_id=i,
            base_date="2024-01-01",
            probability=0.1,
            log_moic_mu=0.0,
            log_moic_sigma=1.0,
            realized_return=0.0,
            settlement="market",
            market_cap=1e8,
        )
        for i in range(20)
    ]
    result = stratify_kpis(obs, key=lambda o: o.market_cap, target_moic=10.0, horizon_years=1.0, model_horizon_years=7.0)
    assert result.get("insufficient") is True
