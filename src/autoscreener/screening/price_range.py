"""52週レンジ内の位置(J-3、investment_decision_gap_2026-08-29.md)。

`price_snapshots` の直近1年の終値から高値・安値と、現在値のレンジ内位置
(0〜1)を出す。表示専用——順位計算には使わない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceRange:
    week52_high: float
    week52_low: float
    # 現在値のレンジ内位置(0.0=安値、1.0=高値)。高値=安値のときは None
    # (0 除算を避け、「値動きがない」を明示する)。
    position_in_range: float | None


def compute_price_range(closes: Sequence[float]) -> PriceRange | None:
    """直近1年ぶんの終値(古い順・新しい順どちらでもよい)から52週レンジを出す。

    最後の要素を現在値として扱う。正の終値が1件も無ければ None。
    """
    positive = [float(c) for c in closes if c is not None and float(c) > 0]
    if not positive:
        return None
    high = max(positive)
    low = min(positive)
    current = positive[-1]
    if high == low:
        return PriceRange(week52_high=high, week52_low=low, position_in_range=None)
    return PriceRange(
        week52_high=high,
        week52_low=low,
        position_in_range=max(0.0, min(1.0, (current - low) / (high - low))),
    )
