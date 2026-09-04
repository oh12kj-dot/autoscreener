"""PIT-bounded macro exposure snapshots for the display-only Live layer."""

from __future__ import annotations

import datetime
import math

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.coverage import CoverageReasonCode, CoverageStatus
from autoscreener.db.models import LiveDatasetCoverage, MacroExposureSnapshot, MacroSeries, PriceSnapshot, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.investment_intelligence import macro_exposure


_RATE_FACTORS = frozenset({"DGS10", "DFII10", "BAMLH0A0HYM2"})
# The stored FRED observations currently have no vintage/realtime boundary.
# They are valid for forward shadow monitoring, but must not be replayed into a
# historical score until ALFRED/FRED vintage collection is implemented.
FRED_VINTAGE_SUPPORTED = False


def _weekly_last(rows: list[tuple[datetime.date, float]]) -> dict[tuple[int, int], tuple[datetime.date, float]]:
    weekly: dict[tuple[int, int], tuple[datetime.date, float]] = {}
    for observed_date, value in rows:
        if value is None or not math.isfinite(float(value)):
            continue
        key = observed_date.isocalendar()[:2]
        current = weekly.get(key)
        if current is None or observed_date > current[0]:
            weekly[key] = (observed_date, float(value))
    return weekly


def _returns(weekly: dict[tuple[int, int], tuple[datetime.date, float]], *, difference: bool) -> dict[tuple[int, int], float]:
    output: dict[tuple[int, int], float] = {}
    previous: float | None = None
    for key in sorted(weekly):
        value = weekly[key][1]
        if previous is not None:
            if difference:
                output[key] = value - previous
            elif previous != 0:
                output[key] = value / previous - 1
        previous = value
    return output


def collect_macro_exposure(*, symbols: list[str] | None = None, observed_at: datetime.datetime | None = None,
                           minimum_weeks: int = 52) -> dict[str, int]:
    """Compute weekly beta only from prices/factors available at ``observed_at``.

    This operation reads stored data only; it never fetches a price or FRED
    series while a detail endpoint is being viewed.
    """
    observed_at = observed_at or datetime.datetime.now(datetime.timezone.utc)
    as_of = observed_at.date()
    counts = {"targets": 0, "snapshots": 0, "with_data": 0, "no_finding": 0, "failed": 0}
    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([symbol.upper() for symbol in symbols])).all()
        else:
            from autoscreener.config import load_edgar_config
            tickers = select_tracked_tickers(session, limit=load_edgar_config().max_tracked_tickers)
        counts["targets"] = len(tickers)
        factors = [row[0] for row in session.query(MacroSeries.series_id).distinct().all()]

        # S-6(daily_pipeline_throughput_plan_2026-09-04):以前はこの系列取得と
        # `_returns`変換をticker(299件)ループの**内側**でfactor(4件)ごとに
        # 毎回やり直していた——クエリ自体はどのtickerでも同じ結果を返すので、
        # 299×4=1,196回の全系列クエリのうち1,192回は無駄な再取得だった
        # (13分の大半がこれ)。ticker ループの外へ括り出して4回に減らす。
        # 出力は1ビットも変わらない(純粋な計算の重複除去)。
        factor_data: dict[str, tuple[dict[tuple[int, int], float], dict[tuple[int, int], tuple[datetime.date, float]]]] = {}
        for factor in factors:
            series = session.query(MacroSeries.observation_date, MacroSeries.value).filter(
                MacroSeries.series_id == factor, MacroSeries.observation_date <= as_of, MacroSeries.value.isnot(None),
            ).order_by(MacroSeries.observation_date).all()
            weekly_series = _weekly_last(series)
            factor_returns = _returns(weekly_series, difference=factor in _RATE_FACTORS)
            factor_data[factor] = (factor_returns, weekly_series)

        for ticker in tickers:
            try:
                prices = session.query(PriceSnapshot.trade_date, PriceSnapshot.close).filter(
                    PriceSnapshot.ticker_id == ticker.id, PriceSnapshot.trade_date <= as_of,
                    PriceSnapshot.close.isnot(None),
                ).order_by(PriceSnapshot.trade_date).all()
                # 同じくticker毎に1回だけ計算し、下のobservation_end算出でも
                # 使い回す(以前はfactorループの中で毎回`_weekly_last(prices)`を
                # 再計算していた)。
                weekly_prices = _weekly_last(prices)
                price_returns = _returns(weekly_prices, difference=False)
                if len(price_returns) < minimum_weeks:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_NO_FINDING,
                            CoverageReasonCode.INSUFFICIENT_PRICE_HISTORY, f"{len(price_returns)} weekly returns")
                    counts["no_finding"] += 1
                    continue
                any_snapshot = False
                insufficient_factor = False
                for factor in factors:
                    factor_returns, weekly_series = factor_data[factor]
                    common = sorted(set(price_returns) & set(factor_returns))
                    if len(common) < minimum_weeks:
                        insufficient_factor = True
                        continue
                    result = macro_exposure([price_returns[key] for key in common], [factor_returns[key] for key in common])
                    if result["beta"] is None:
                        insufficient_factor = True
                        continue
                    observation_end = min(max(weekly_prices[key][0] for key in common), max(weekly_series[key][0] for key in common))
                    exists = session.query(MacroExposureSnapshot.id).filter_by(
                        ticker_id=ticker.id, factor=factor, observed_at=observed_at
                    ).first()
                    if exists is None:
                        session.add(MacroExposureSnapshot(ticker_id=ticker.id, observed_at=observed_at, observation_end=observation_end,
                            factor=factor, beta=result["beta"], downside_beta=result["downside_beta"], sample_count=result["sample_count"],
                            source="price_snapshots+fred", source_url=None, coverage_status=CoverageStatus.COLLECTED_WITH_DATA,
                            confidence="medium", raw_payload={"series_id": factor, "transform": "difference" if factor in _RATE_FACTORS else "return",
                            "window_weeks": len(common),
                            "fred_vintage_supported": FRED_VINTAGE_SUPPORTED,
                            "historical_backtest_supported": FRED_VINTAGE_SUPPORTED,
                            "forward_shadow_only": not FRED_VINTAGE_SUPPORTED,
                        }))
                        counts["snapshots"] += 1
                    any_snapshot = True
                if any_snapshot:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_WITH_DATA, None, None)
                    counts["with_data"] += 1
                else:
                    _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTED_NO_FINDING,
                            CoverageReasonCode.INSUFFICIENT_FACTOR_HISTORY if insufficient_factor else CoverageReasonCode.SOURCE_NOT_SCANNED,
                            "No factor had a usable 52-week aligned sample")
                    counts["no_finding"] += 1
            except Exception as exc:
                _ledger(session, ticker.id, observed_at, CoverageStatus.COLLECTION_FAILED, CoverageReasonCode.DATABASE_ERROR,
                        type(exc).__name__, retryable=True)
                counts["failed"] += 1
    return counts


def _ledger(session, ticker_id: int, observed_at: datetime.datetime, status: CoverageStatus,
            reason_code: CoverageReasonCode | None, reason_detail: str | None, retryable: bool | None = None) -> None:
    session.add(LiveDatasetCoverage(ticker_id=ticker_id, dataset="macro_exposure", observed_at=observed_at,
        attempted_at=observed_at, source="price_snapshots+fred", source_scope="weekly PIT prices and configured FRED factors",
        coverage_status=status, reason_code=reason_code, reason_detail=reason_detail, retryable=retryable, confidence="medium"))
