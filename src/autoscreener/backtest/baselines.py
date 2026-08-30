"""単純ベースラインとの比較(defect_and_edge_audit_2026-08-28.md D-8)。

**このアプリはモメンタム単独のベースラインを一度も測っていない。** 約1,000行の
v4 モデル(成長・粗利率・希薄化・マルチプル圧縮・生存確率・σ縮小)が、
「12ヶ月モメンタムで並べる」(1行)に勝っているという証拠がどこにも無い。
`rank_ic = 0.152` は、マイクロキャップの12ヶ月モメンタム単独でも同程度が出うる。

`run-backtest` はこれらのベースラインを**同一観測に対して**評価し、v4 が
`momentum_12m` と `revenue_growth` の**両方**に、D-2 のブートストラップCIを超える
差で勝つかを見る。勝てないなら、それが最も重要な発見である。
"""

from __future__ import annotations

import random
import statistics

from autoscreener.backtest.metrics import Observation, _cross_sectional_buckets, per_date_stats, spearman, _weighted_mean


def _momentum(inp, cs, cfg) -> float | None:
    return inp.log_momentum_12m


def _revenue_growth(inp, cs, cfg) -> float | None:
    g = inp.revenue_cagr if inp.revenue_cagr is not None else inp.revenue_yoy
    return g


def _cheapness(inp, cs, cfg) -> float | None:
    gp = inp.gross_profit_latest
    if gp is None or gp <= 0:
        return None
    ev = inp.market_cap + inp.net_debt
    return -(ev / gp)  # 安いほど高スコア


def _gross_profit_scale(inp, cs, cfg) -> float | None:
    return inp.gross_profit_latest


_RNG = random.Random(20260828)


def _random(inp, cs, cfg) -> float:
    return _RNG.random()


BASELINES = {
    "momentum_12m": _momentum,
    "revenue_growth": _revenue_growth,
    "cheapness": _cheapness,
    "gross_profit_scale": _gross_profit_scale,
    "random": _random,
}


def baseline_metrics(
    observations: list[Observation],
    target_moic: float,
    horizon_years: float,
    model_horizon_years: float,
    decile_count: int = 10,
) -> dict[str, dict]:
    """各ベースラインについて lift / decile_monotonicity / rank_ic を返す。

    `observations` の各要素は `baseline_scores`(name -> score)を持つこと。
    score が None の観測はそのベースラインのランキングから外す。
    """
    from autoscreener.backtest.metrics import on_pace_threshold

    threshold = on_pace_threshold(target_moic, horizon_years, model_horizon_years)
    out: dict[str, dict] = {}
    for name in BASELINES:
        scored = [
            Observation(
                ticker_id=o.ticker_id,
                base_date=o.base_date,
                probability=o.baseline_scores[name],
                log_moic_mu=o.log_moic_mu,
                log_moic_sigma=o.log_moic_sigma,
                realized_return=o.realized_return,
                settlement=o.settlement,
            )
            for o in observations
            if o.baseline_scores.get(name) is not None
        ]
        if len(scored) < decile_count * 2:
            continue
        buckets = _cross_sectional_buckets(scored, decile_count)
        deciles = [
            (i + 1, statistics.median([x.realized_return for x in b]))
            for i, b in enumerate(buckets)
            if b
        ]
        monotonicity = spearman([-d for d, _ in deciles], [m for _, m in deciles])
        returns = [x.realized_return for x in scored]
        universe_on_pace = (
            sum(1 for r in returns if 1 + r >= threshold) / len(returns) if returns else 0.0
        )
        top = buckets[0] if buckets and buckets[0] else []
        top_rate = (
            sum(1 for x in top if 1 + x.realized_return >= threshold) / len(top) if top else 0.0
        )
        lift = top_rate / universe_on_pace if universe_on_pace > 0 else 0.0
        pds = per_date_stats(scored, threshold)
        rank_ic = _weighted_mean([d.rank_ic for d in pds], [d.count for d in pds])
        out[name] = {
            "lift_ratio": lift,
            "decile_monotonicity": monotonicity,
            "rank_ic": rank_ic,
            "n": len(scored),
        }
    return out
