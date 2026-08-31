"""バリュエーションの現在地(J-3、docs/investment_decision_gap_2026-08-29.md)。

`current_ev_to_gross_profit` が 12.4 だとして、それが**高いのか安いのか**を、
同じ日の断面での分位で示す。

**これはモデルの入力ではない。** κ による「成長の対価」の差し引き(28.2)は
`moic.py` 内部で既に行われている。ここで計算するのは人間が読むための断面情報
だけで、順位計算(`compute_moic` / `probability`)には一切影響させない
(J-3 受け入れ基準)。分位は「同じ日の断面」でのみ切る——日付をまたいで
プールしない(D-4 の再発防止)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# 14.7:セクター内標本がこれ未満なら相対分位を出さない(頑健性が無い)。
SECTOR_MIN_SAMPLE = 20


def percentile_of(values: Sequence[float], x: float) -> float | None:
    """`values` の分布における `x` の分位(0.0〜1.0)。中順位法。

    標本内の最小値はちょうど 0.0、最大値はちょうど 1.0 になる
    (`rank / (n - 1)`)。同値は中順位で割り当てる。標本が2未満なら None。
    """
    ordered = sorted(values)
    n = len(ordered)
    if n < 2:
        return None
    below = sum(1 for v in ordered if v < x)
    equal = sum(1 for v in ordered if v == x)
    rank = below + (equal - 1) / 2 if equal else float(below)
    return max(0.0, min(1.0, rank / (n - 1)))


@dataclass(frozen=True)
class TickerValuationRow:
    ticker_id: int
    sector: str | None
    ev_to_gross_profit: float | None
    revenue_growth: float | None
    gross_margin: float | None


def _percentiles_for_metric(
    rows: list[TickerValuationRow],
    value_of,
    within_sector: bool,
) -> dict[int, float | None]:
    """1指標について、全銘柄の分位を返す。

    `within_sector=True` なら各銘柄を**自分のセクターの標本**の中で切る。
    セクター標本(その指標が非 None の件数)が `SECTOR_MIN_SAMPLE` 未満の
    銘柄には None を入れる。
    """
    result: dict[int, float | None] = {}
    if within_sector:
        by_sector: dict[str | None, list[float]] = {}
        for row in rows:
            v = value_of(row)
            if v is not None:
                by_sector.setdefault(row.sector, []).append(v)
        for row in rows:
            v = value_of(row)
            sample = by_sector.get(row.sector, [])
            if v is None or row.sector is None or len(sample) < SECTOR_MIN_SAMPLE:
                result[row.ticker_id] = None
            else:
                result[row.ticker_id] = percentile_of(sample, v)
    else:
        sample = [value_of(r) for r in rows if value_of(r) is not None]
        for row in rows:
            v = value_of(row)
            result[row.ticker_id] = None if v is None else percentile_of(sample, v)
    return result


def compute_valuation_percentiles(
    rows: list[TickerValuationRow],
) -> dict[int, dict[str, float | None]]:
    """当日の断面から、銘柄ごとのバリュエーション分位を返す。

    キー:
      - ev_to_gross_profit_percentile_universe
      - ev_to_gross_profit_percentile_sector
      - revenue_growth_percentile_sector
      - gross_margin_percentile_sector

    値は 0.0〜1.0 か None(欠損・セクター標本不足)。呼び出し元は None のキーを
    `factors` から落としてよい(UI は「セクター標本が少ないため非表示」を出す)。
    """
    ev_universe = _percentiles_for_metric(rows, lambda r: r.ev_to_gross_profit, within_sector=False)
    ev_sector = _percentiles_for_metric(rows, lambda r: r.ev_to_gross_profit, within_sector=True)
    growth_sector = _percentiles_for_metric(rows, lambda r: r.revenue_growth, within_sector=True)
    margin_sector = _percentiles_for_metric(rows, lambda r: r.gross_margin, within_sector=True)

    out: dict[int, dict[str, float | None]] = {}
    for row in rows:
        out[row.ticker_id] = {
            "ev_to_gross_profit_percentile_universe": ev_universe.get(row.ticker_id),
            "ev_to_gross_profit_percentile_sector": ev_sector.get(row.ticker_id),
            "revenue_growth_percentile_sector": growth_sector.get(row.ticker_id),
            "gross_margin_percentile_sector": margin_sector.get(row.ticker_id),
        }
    return out
