"""マルチプルの成長弾力性 κ の推定(28.2)。すべて純粋関数。

**このモジュールが推定するのは「市場の値づけ規則」であって、リターン予測ではない。**

    ln(EV / 粗利) = 定数 + κ · g

κ は「成長率が1ポイント高い企業は、EV/粗利が何%高く評価されているか」を表す。
`moic.growth_fade_multiple_change` はこれを1点だけに使う——モデルが7年かけて
成長を g₀ から g_H へ減速させる以上、整合的なマルチプル変化は exp(κ·(g_H − g₀))
である、という**内部整合性の要請**である。

**なぜリターンに較正しないのか。** κ をリターンにフィットさせると、その時期に
たまたま高成長株が報われたかどうかを固定してしまう(2024〜2025年の標本では
κ を大きくするほどリフトが上がる)。断面のバリュエーション構造から測れば、
その値は「市場が今どう値づけしているか」という**観測可能な事実**であり、
リターンのレジームに依存しない。

**実測(2026-08-26、価格ヒストリー全期間の20断面、各700〜800銘柄)**:
κ は +0.68 〜 +1.10 に収まり、平均 +0.863、断面間の標準偏差 0.117、
t値はどの断面でも +3.5 〜 +6.3 だった。決定係数は0.02〜0.06と低いが、
これは想定どおりである——EV/粗利のばらつきの大半は成長率以外の要因
(利益率構造・セクター・センチメント)で決まる。ここで必要なのは
**傾きの符号と大きさ**であって、説明力ではない。

**ユニバースを変えたら測り直すこと。** 米国以外の市場、あるいは時価総額の
レンジを変えれば、値づけの構造そのものが変わる。
`uv run python -m autoscreener.cli estimate-elasticity` で再推定できる。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

# EV/粗利の裾は極端に重い(実データで中央値2.09に対し最大638.7)。対数を取っても
# 残る外れ値は回帰の傾きを支配するため、この範囲の外は落とす。
_MIN_MULTIPLE = 0.2
_MAX_MULTIPLE = 200.0

# 傾きを推定するのに最低限必要な観測数
_MIN_SAMPLE = 50

# 複数断面をまとめるときに採用する最小観測数。価格ヒストリーの先頭付近は可視な
# 年次期数が足りず数十銘柄しか残らず、そこでの推定は標準誤差が0.5を超える。
# 実データでは 2024-01(n=76)と 2024-02(n=72)が κ≈1.83 という外れ値を出し、
# 平均を 0.86 から 0.95 へ押し上げていた。厚い断面だけで平均を取る。
MIN_POOLING_SAMPLE = 200


@dataclass(frozen=True)
class ElasticityEstimate:
    """1断面分の推定結果。"""

    slope: float  # κ
    intercept: float
    standard_error: float
    sample_size: int
    r_squared: float

    @property
    def t_statistic(self) -> float:
        return self.slope / self.standard_error if self.standard_error > 0 else 0.0


def estimate_growth_elasticity(
    observations: list[tuple[float, float]],
) -> ElasticityEstimate | None:
    """(成長率, EV/粗利) の組から κ を推定する。単純最小二乗。

    `observations` の第2要素は**倍率そのもの**(対数ではない)。ここで対数を取り、
    極端な倍率を落とす前処理も内部で行う——呼び出し側でトリミングの条件が
    ばらつくと、断面ごとの推定値が比較できなくなるため。
    """
    points = [
        (growth, math.log(multiple))
        for growth, multiple in observations
        if multiple is not None
        and _MIN_MULTIPLE < multiple < _MAX_MULTIPLE
        and growth is not None
        and math.isfinite(growth)
    ]
    if len(points) < _MIN_SAMPLE:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    residuals = [y - (intercept + slope * x) for x, y in points]
    degrees_of_freedom = len(points) - 2
    if degrees_of_freedom <= 0:
        return None
    residual_variance = sum(r**2 for r in residuals) / degrees_of_freedom
    standard_error = math.sqrt(residual_variance / sxx)

    syy = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1 - (sum(r**2 for r in residuals) / syy) if syy > 0 else 0.0

    return ElasticityEstimate(
        slope=slope,
        intercept=intercept,
        standard_error=standard_error,
        sample_size=len(points),
        r_squared=r_squared,
    )


def pool_estimates(estimates: list[ElasticityEstimate]) -> tuple[float, float] | None:
    """複数断面の推定値をまとめる。戻り値は (平均κ, 断面間の標準偏差)。

    断面ごとの標準誤差ではなく**断面間のばらつき**を返すのが要点である。
    同じ日の700銘柄は独立ではないので、その中での標準誤差は検出力を過大に
    見せる。「日を変えても同じ値が出るか」のほうが、この構造パラメータを
    信用してよいかの判断材料になる。

    観測数が `MIN_POOLING_SAMPLE` に満たない断面は平均から外す。銘柄数が
    数十しかない断面の傾きは標準誤差が0.5を超え、平均を数珠つなぎに引っ張る。
    """
    slopes = [e.slope for e in estimates if e.sample_size >= MIN_POOLING_SAMPLE]
    if len(slopes) < 2:
        return None
    return statistics.fmean(slopes), statistics.stdev(slopes)
