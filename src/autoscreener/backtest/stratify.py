"""KPIの層別(docs/defect_and_edge_audit_2026-08-28.md I-4 / 28.9)。純粋関数。

**I-4【高】浮動株比率とインサイダー保有** — `dei:EntityPublicFloat`。

    insider_and_affiliate_ownership ≈ 1 − EntityPublicFloat / market_cap

**この1つの分析が、「プロに勝てるか」という問いに対する最も直接的な答えになる。**
モデルのリフトが「浮動株が小さい層でだけ立っている」なら、それがこのアプリの
edge の正体であり、ユニバース定義をそちらへ寄せるべき。逆に浮動株の大きい層で
リフトが立っているなら、プロと同じ土俵で戦っており edge の主張は成立しない。

`stratify_kpis` は任意のキー(浮動株比率・時価総額・セクター等)で観測を5分位に
切り、分位ごとに lift / decile_monotonicity / rank_ic を出す汎用関数。
浮動株比率が `MoicInputs` に入る(I-1 段階)まで、`market_cap` 等で使える。
"""

from __future__ import annotations

import statistics

from autoscreener.backtest.metrics import (
    Observation,
    _cross_sectional_buckets,
    _weighted_mean,
    on_pace_threshold,
    per_date_stats,
    spearman,
)


def _quantile_edges(values: list[float], n: int) -> list[float]:
    ordered = sorted(values)
    return [ordered[min(len(ordered) - 1, round(i / n * (len(ordered) - 1)))] for i in range(1, n)]


def _bucket_of(value: float, edges: list[float]) -> int:
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


def _kpis_for(observations: list[Observation], threshold: float, decile_count: int) -> dict:
    if len(observations) < decile_count * 2:
        return {"n": len(observations), "insufficient": True}
    buckets = _cross_sectional_buckets(observations, decile_count)
    deciles = [
        (i + 1, statistics.median([x.realized_return for x in b]))
        for i, b in enumerate(buckets)
        if b
    ]
    returns = [o.realized_return for o in observations]
    universe_on_pace = sum(1 for r in returns if 1 + r >= threshold) / len(returns)
    top = buckets[0] if buckets and buckets[0] else []
    top_rate = (
        sum(1 for x in top if 1 + x.realized_return >= threshold) / len(top) if top else 0.0
    )
    pds = per_date_stats(observations, threshold)
    return {
        "n": len(observations),
        "lift_ratio": (top_rate / universe_on_pace) if universe_on_pace > 0 else 0.0,
        "decile_monotonicity": spearman([-d for d, _ in deciles], [m for _, m in deciles]),
        "rank_ic": _weighted_mean([d.rank_ic for d in pds], [d.count for d in pds]),
        "universe_on_pace_rate": universe_on_pace,
        "universe_loss_rate": sum(1 for r in returns if r <= -0.5) / len(returns),
    }


def stratify_kpis(
    observations: list[Observation],
    key,
    target_moic: float,
    horizon_years: float,
    model_horizon_years: float,
    n_buckets: int = 5,
    decile_count: int = 10,
) -> dict:
    """`key(observation) -> float` で観測を `n_buckets` 分位に切り、分位ごとにKPIを出す。

    `key` が None を返す観測は除外する。`bucket_0` が最小、`bucket_{n-1}` が最大。
    """
    threshold = on_pace_threshold(target_moic, horizon_years, model_horizon_years)
    keyed = [(o, key(o)) for o in observations]
    keyed = [(o, v) for o, v in keyed if v is not None]
    if len(keyed) < n_buckets * decile_count * 2:
        return {"insufficient": True, "n": len(keyed)}
    edges = _quantile_edges([v for _, v in keyed], n_buckets)
    grouped: dict[int, list[Observation]] = {i: [] for i in range(n_buckets)}
    for o, v in keyed:
        grouped[_bucket_of(v, edges)].append(o)
    return {
        f"bucket_{i}": _kpis_for(grouped[i], threshold, decile_count) for i in range(n_buckets)
    }
