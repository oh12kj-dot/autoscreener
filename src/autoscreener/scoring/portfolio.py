"""ポートフォリオ水準の確率(28.12)。すべて純粋関数。

**なぜ必要か。** 銘柄ごとの P(10倍) を並べただけでは、利用者が実際に知りたい
「上位20銘柄を買ったら、そのうち1つでも10倍になる確率はどれくらいか」に
答えられない。そして利用者はこれを自分で計算しようとすると、ほぼ確実に

    P(少なくとも1つ) = 1 − Π(1 − p_i)

という**独立性を仮定した式**を使う。これは楽観に偏る。10バガーの発生は
銘柄ごとに独立ではなく、マクロ環境・金利・セクターの循環という共通因子に
強く支配されているからである。実測でも、擬似バックテストの評価日ごとの
オンペース率は 17% 〜 45% と大きく振れており、これは各銘柄が独立にコインを
投げているなら起こりえない散らばりである(二項分布のノイズをはるかに超える)。

**共通因子は「1つでも当たる確率」を下げる。** 相関が正だと結果は固まる——
当たる年は多くの銘柄が当たり、外れる年はどれも外れる。同じ期待本数でも、
「少なくとも1つ」の確率は独立の場合より低くなる。20銘柄持っても20回の
独立な試行にはならない、という事実こそが伝えるべき情報である。

**モデル:1ファクターのガウシアン・コピュラ(Vasicek)。** 銘柄 i は

    √ρ · M + √(1−ρ) · ε_i  >  Φ⁻¹(1 − p_i)

のとき当たる。M は全銘柄共通の潜在因子、ε_i は銘柄固有。ρ は資産相関で、
**評価日ごとのオンペース率の散らばりから推定する**(`estimate_asset_correlation`)。
信用ポートフォリオで標準的に使われる構造であり、相関を1つのパラメータで
表す最小限の仮定になっている。
"""

from __future__ import annotations

import math
import datetime
from dataclasses import dataclass
from typing import Mapping, Sequence
from statistics import NormalDist

_NORMAL = NormalDist()

# 共通因子 M についての数値積分の格子。±6σ を 241 点で刻む。
_GRID_LIMIT = 6.0
_GRID_POINTS = 241

# 資産相関の探索範囲。0 は完全独立、1 は全銘柄が同時に当たる/外れる。
_MIN_RHO = 0.0
_MAX_RHO = 0.95


@dataclass(frozen=True)
class PortfolioOutcome:
    """上位 N 銘柄をまとめて持ったときの見通し。"""

    holdings: int
    asset_correlation: float
    expected_hits: float  # 期待本数 Σp_i。相関に依存しない
    probability_at_least_one: float
    probability_at_least_one_if_independent: float  # 相関を無視した場合(比較用)
    probability_at_least_two: float


def pairwise_return_correlation(
    series_by_ticker: Mapping[str, Sequence[tuple[datetime.date, float]]], *, min_overlap: int = 120
) -> dict[tuple[str, str], tuple[float, int]]:
    """Daily log-return correlations, joining each pair by date.

    This is intentionally separate from the backtest-derived asset correlation.
    """
    returns: dict[str, dict[datetime.date, float]] = {}
    for ticker, series in series_by_ticker.items():
        ordered = sorted((d, float(v)) for d, v in series if v is not None and v > 0)
        returns[ticker] = {d: math.log(v / prior) for (_, prior), (d, v) in zip(ordered, ordered[1:])}
    result: dict[tuple[str, str], tuple[float, int]] = {}
    symbols = sorted(returns)
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            dates = sorted(returns[a].keys() & returns[b].keys())
            if len(dates) < min_overlap: continue
            xs, ys = [returns[a][d] for d in dates], [returns[b][d] for d in dates]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            denom = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
            if denom > 0:
                result[(a, b)] = (sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / denom, len(dates))
    return result


def _factor_grid() -> list[tuple[float, float]]:
    """(M の値, 正規密度の重み) の格子。重みは合計1に正規化する。"""
    step = 2 * _GRID_LIMIT / (_GRID_POINTS - 1)
    nodes = [-_GRID_LIMIT + step * i for i in range(_GRID_POINTS)]
    weights = [math.exp(-m * m / 2) for m in nodes]
    total = sum(weights)
    return [(m, w / total) for m, w in zip(nodes, weights)]


def conditional_hit_probability(probability: float, factor: float, rho: float) -> float:
    """共通因子が `factor` のときの、その銘柄の当たり確率。"""
    probability = min(max(probability, 1e-12), 1 - 1e-12)
    if rho <= 0:
        return probability
    if rho >= 1:
        return 1.0 if factor > -_NORMAL.inv_cdf(probability) else 0.0
    threshold = _NORMAL.inv_cdf(probability)
    return _NORMAL.cdf((threshold + math.sqrt(rho) * factor) / math.sqrt(1 - rho))


def portfolio_outcome(probabilities: list[float], rho: float) -> PortfolioOutcome:
    """上位 N 銘柄について「少なくとも1つ当たる」確率などを返す。

    共通因子 M を数値積分で潰す。M を固定すれば銘柄どうしは条件付き独立に
    なるので、その下では `Π(1 − p_i|M)` がそのまま「1つも当たらない」確率になる。
    """
    probabilities = [min(max(p, 0.0), 1.0) for p in probabilities]
    if not probabilities:
        return PortfolioOutcome(0, rho, 0.0, 0.0, 0.0, 0.0)

    rho = min(max(rho, _MIN_RHO), _MAX_RHO)

    none_total = 0.0
    exactly_one_total = 0.0
    for factor, weight in _factor_grid():
        conditional = [conditional_hit_probability(p, factor, rho) for p in probabilities]
        none = 1.0
        for p in conditional:
            none *= 1 - p
        none_total += weight * none
        # ちょうど1つ当たる確率 = Σ_i p_i Π_{j≠i}(1−p_j)
        exactly_one = 0.0
        for p in conditional:
            if p < 1.0:
                exactly_one += none * p / (1 - p)
        exactly_one_total += weight * exactly_one

    independent_none = 1.0
    for p in probabilities:
        independent_none *= 1 - p

    at_least_one = 1 - none_total
    return PortfolioOutcome(
        holdings=len(probabilities),
        asset_correlation=rho,
        expected_hits=sum(probabilities),
        probability_at_least_one=at_least_one,
        probability_at_least_one_if_independent=1 - independent_none,
        probability_at_least_two=max(0.0, at_least_one - exactly_one_total),
    )


def estimate_asset_correlation(per_date_hit_rates: list[float], counts: list[int]) -> float | None:
    """評価日ごとの的中率の散らばりから資産相関 ρ を推定する。

    各評価日を「共通因子が1回引かれた実現」とみなす。銘柄が独立なら、日ごとの
    的中率のばらつきは二項分布のノイズ `p(1−p)/n` に収まるはずである。実際には
    それをはるかに超えて振れる——その超過分が共通因子の効き具合、すなわち ρ に
    対応する。

    ρ を動かしながら「理論上の的中率の分散」を数値積分で出し、実測の分散に
    最も近い ρ を二分探索で選ぶ。二項ノイズの分だけは実測分散から差し引く。

    **限界**:評価日は7〜9個しかなく、しかも保有期間が重なっているため独立では
    ない(27.18)。この ρ は桁の目安であって精密な推定ではない。それでも
    「独立と仮定するよりは桁が合う」ことに価値がある。
    """
    usable = [(r, n) for r, n in zip(per_date_hit_rates, counts) if n > 0]
    if len(usable) < 3:
        return None

    rates = [r for r, _ in usable]
    mean_rate = sum(rates) / len(rates)
    if not 0 < mean_rate < 1:
        return None

    observed_variance = sum((r - mean_rate) ** 2 for r in rates) / (len(rates) - 1)
    mean_count = sum(n for _, n in usable) / len(usable)
    binomial_noise = mean_rate * (1 - mean_rate) / mean_count
    systematic_variance = observed_variance - binomial_noise
    if systematic_variance <= 0:
        return 0.0

    def theoretical_variance(rho: float) -> float:
        grid = _factor_grid()
        first = sum(w * conditional_hit_probability(mean_rate, m, rho) for m, w in grid)
        second = sum(w * conditional_hit_probability(mean_rate, m, rho) ** 2 for m, w in grid)
        return second - first**2

    low, high = _MIN_RHO, _MAX_RHO
    if theoretical_variance(high) < systematic_variance:
        return high
    for _ in range(60):
        mid = (low + high) / 2
        if theoretical_variance(mid) < systematic_variance:
            low = mid
        else:
            high = mid
    return (low + high) / 2
