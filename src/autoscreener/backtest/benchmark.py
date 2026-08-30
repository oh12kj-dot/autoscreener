"""Rolling benchmark return distribution for comparison only (L-9)."""
from __future__ import annotations
from typing import Sequence
import datetime

def rolling_moic_quantiles(closes: Sequence[tuple[datetime.date, float]], horizon_years: float) -> dict[str, float] | None:
    ordered = sorted((d, float(v)) for d, v in closes if v is not None and v > 0)
    days = max(1, round(252 * horizon_years))
    values = [end / start for (_, start), (_, end) in zip(ordered, ordered[days:])]
    if not values: return None
    values.sort()
    def q(p: float) -> float:
        index = (len(values) - 1) * p
        lo, hi = int(index), min(int(index) + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (index - lo)
    return {f"p{int(p*100)}": q(p) for p in (0.10, 0.25, 0.50, 0.75, 0.90)}
