"""上場廃止ハザード率の実測較正(docs/defect_and_edge_audit_2026-08-28.md D-9)。純粋関数。

`config/scoring.yaml` の `survival.base_annual_hazard = 0.06` /
`health_sensitivity = 1.2` は「(b) 公表された基準率からの事前値」であり、
標本に廃止が0件(D-1)なので**検証手段がゼロ**だった。D-1(上場廃止ユニバース)
が入れば、「評価日から1年以内に廃止されたか」を目的変数、`health_index` を
説明変数にしたロジスティック回帰で実測値を出せる。

    logit P(1年以内に廃止) = a + b * health_index

`moic.py` のハザードモデルは
    annual_hazard = base_annual_hazard * exp(-health_sensitivity * health_index)
という形なので、対応づけは

    base_annual_hazard  ≈ sigmoid(a)                     (health_index = 0 での年率)
    health_sensitivity  ≈ -b * (1 - p0)                  (p0 = sigmoid(a); ロジット係数を
                                                          ハザード比の対数近似へ変換)

**買収による廃止と破綻による廃止は分けること**(D-9)。前者は10バガーの経路を
途中で断つが −100% ではない。呼び出し元が `event` を "bankruptcy" / "acquisition"
で切り分けて別々に推定する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_MAX_ITERATIONS = 50
_TOLERANCE = 1e-8


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass(frozen=True)
class HazardEstimate:
    intercept: float  # a
    health_coef: float  # b (health_index に対するロジット係数。負なら健全ほど廃止しにくい)
    n: int
    n_events: int
    base_annual_hazard: float  # sigmoid(a) — health_index=0 での1年廃止確率
    health_sensitivity: float  # moic.py のハザードモデルへ渡す形へ変換した値
    converged: bool


def fit_logistic(
    xs: list[float], ys: list[float], *, ridge: float = 1e-4
) -> tuple[float, float, bool]:
    """1変数ロジスティック回帰を Newton–Raphson で解く。返り値 (intercept, coef, converged)。

    小標本・完全分離に備えて弱いリッジ(L2)正則化を入れる。`ys` は 0/1。
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0, 0.0, False
    a, b = 0.0, 0.0
    n = len(xs)
    for _ in range(_MAX_ITERATIONS):
        g0 = g1 = 0.0
        h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(a + b * x)
            w = max(p * (1.0 - p), 1e-9)
            r = p - y
            g0 += r
            g1 += r * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        # リッジ項(切片は正則化しない)。
        g1 += ridge * b
        h11 += ridge
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            return a, b, False
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a -= da
        b -= db
        if abs(da) < _TOLERANCE and abs(db) < _TOLERANCE:
            return a, b, True
    return a, b, False


def estimate_hazard(
    observations: list[tuple[float, bool]],
) -> HazardEstimate | None:
    """`(health_index, delisted_within_1y)` の観測群からハザード・パラメータを推定する。

    観測が3未満、またはイベント(廃止)が1件も無ければ None(D-1 未完了の状態)。
    """
    usable = [(h, 1.0 if d else 0.0) for h, d in observations if h is not None]
    if len(usable) < 3:
        return None
    n_events = int(sum(y for _, y in usable))
    if n_events == 0:
        return None
    xs = [h for h, _ in usable]
    ys = [y for _, y in usable]
    a, b, converged = fit_logistic(xs, ys)
    p0 = _sigmoid(a)
    # ロジット係数 b を「health_index が1増えるとハザードが exp(sensitivity) 倍
    # 減る」形へ。低確率域では logit ≈ log(odds) ≈ log(hazard) なので
    # health_sensitivity ≈ -b。p0 が小さくないときのために (1-p0) で減衰させる。
    health_sensitivity = -b * (1.0 - p0)
    return HazardEstimate(
        intercept=a,
        health_coef=b,
        n=len(usable),
        n_events=n_events,
        base_annual_hazard=p0,
        health_sensitivity=health_sensitivity,
        converged=converged,
    )
