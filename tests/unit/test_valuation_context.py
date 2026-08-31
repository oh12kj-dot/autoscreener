"""J-3(docs/investment_decision_gap_2026-08-29.md):バリュエーション断面分位のテスト。"""

from __future__ import annotations

from autoscreener.scoring.valuation_context import (
    SECTOR_MIN_SAMPLE,
    TickerValuationRow,
    compute_valuation_percentiles,
    percentile_of,
)


def test_percentile_min_is_zero_max_is_one() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile_of(values, 10.0) == 0.0
    assert percentile_of(values, 50.0) == 1.0
    assert 0.0 < percentile_of(values, 30.0) < 1.0


def test_percentile_stays_in_unit_interval_for_out_of_range() -> None:
    values = [1.0, 2.0, 3.0]
    assert percentile_of(values, -5.0) == 0.0
    assert percentile_of(values, 99.0) == 1.0


def test_percentile_none_when_sample_too_small() -> None:
    assert percentile_of([1.0], 1.0) is None
    assert percentile_of([], 1.0) is None


def _rows(n: int, sector: str, *, start: float = 1.0) -> list[TickerValuationRow]:
    return [
        TickerValuationRow(
            ticker_id=i,
            sector=sector,
            ev_to_gross_profit=start + i,
            revenue_growth=0.10 + i / 100,
            gross_margin=0.30 + i / 100,
        )
        for i in range(n)
    ]


def test_sector_percentile_is_none_below_min_sample_and_present_at_min() -> None:
    nineteen = _rows(SECTOR_MIN_SAMPLE - 1, "Tech")
    result = compute_valuation_percentiles(nineteen)
    assert all(
        result[r.ticker_id]["ev_to_gross_profit_percentile_sector"] is None for r in nineteen
    )
    # universe 分位はセクター標本数と無関係に出る
    assert all(
        result[r.ticker_id]["ev_to_gross_profit_percentile_universe"] is not None for r in nineteen
    )

    twenty = _rows(SECTOR_MIN_SAMPLE, "Tech")
    result = compute_valuation_percentiles(twenty)
    values = [result[r.ticker_id]["ev_to_gross_profit_percentile_sector"] for r in twenty]
    assert all(v is not None for v in values)
    assert min(values) == 0.0
    assert max(values) == 1.0


def test_missing_metric_yields_none_for_that_ticker_only() -> None:
    rows = _rows(SECTOR_MIN_SAMPLE, "Tech")
    rows.append(
        TickerValuationRow(
            ticker_id=999, sector="Tech", ev_to_gross_profit=None, revenue_growth=None, gross_margin=None
        )
    )
    result = compute_valuation_percentiles(rows)
    assert result[999]["ev_to_gross_profit_percentile_universe"] is None
    assert result[999]["ev_to_gross_profit_percentile_sector"] is None
    assert result[0]["ev_to_gross_profit_percentile_universe"] is not None
