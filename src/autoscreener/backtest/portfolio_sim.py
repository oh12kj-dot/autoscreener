"""ポートフォリオ・シミュレーション(defect_and_edge_audit_2026-08-28.md D-4)。純粋関数。

**なぜ必要か。** 今のバックテスト(`backtest/metrics.py`)は「観測ごとのリターンの
集計」であって、ポートフォリオを一度も組んでいない。`BacktestMetrics` の
フィールドは全部**ユニバース内の相対量**(lift = 上位デシル率 ÷ ユニバース率、
単調性、順位IC)であり、「このモデルに従ったら何%儲かったのか」「指数に勝ったのか」
に答えられない。上位デシルのリフトが 1.50 でも、ゲート通過ユニバース自体が指数に
負けていれば、このアプリは「負け方が上手い銘柄群」を出しているだけになる。

このモジュールは評価日ごとに上位N銘柄を等金額で建て、次のリバランスで入れ替える
運用を模擬し、**IWC 超過CAGRと最大ドローダウンを1つの数字で**出す。

**CAGR は非重複トランシェから出す(D-2)。** ホライズン間隔以上に離れた評価日
だけを使い、保有期間が重ならないようにする。3年ヒストリー・1年ホライズンなら
3トランシェ——それが正直な検出力である。重複ぶんの平均期間リターンは参考値として
別に返す。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortfolioPosition:
    ticker_id: int
    base_date: str
    sector: str | None
    weight: float
    realized_return: float
    cost_bps: float


@dataclass(frozen=True)
class PortfolioBacktest:
    holdings_per_rebalance: int
    rebalance_dates: list[str]
    # 非重複トランシェ(保有期間が重ならない評価日)から出した数字。
    non_overlapping_tranche_count: int
    equity_curve: list[tuple[str, float]]  # (トランシェ末尾の日付, 資産倍率)
    cagr: float
    max_drawdown: float
    volatility: float  # 期間リターンの標準偏差を年率化
    benchmark_cagr: dict[str, float] = field(default_factory=dict)
    excess_cagr: dict[str, float] = field(default_factory=dict)
    win_rate_vs_benchmark: dict[str, float] = field(default_factory=dict)
    turnover: float = 0.0  # リバランス間で入れ替わった銘柄の割合(全評価日平均)
    realized_cost_drag: float = 0.0  # 取引コストによる年率のリターン低下
    # 全評価日(重複含む)の basket 期間リターンの平均。参考値。
    mean_overlapping_period_return: float = 0.0

    def as_dict(self) -> dict:
        return {
            "holdings_per_rebalance": self.holdings_per_rebalance,
            "rebalance_dates": self.rebalance_dates,
            "non_overlapping_tranche_count": self.non_overlapping_tranche_count,
            "equity_curve": [[d, v] for d, v in self.equity_curve],
            "cagr": self.cagr,
            "max_drawdown": self.max_drawdown,
            "volatility": self.volatility,
            "benchmark_cagr": self.benchmark_cagr,
            "excess_cagr": self.excess_cagr,
            "win_rate_vs_benchmark": self.win_rate_vs_benchmark,
            "turnover": self.turnover,
            "realized_cost_drag": self.realized_cost_drag,
            "mean_overlapping_period_return": self.mean_overlapping_period_return,
        }


@dataclass(frozen=True)
class SimObservation:
    """`backtest.metrics.Observation` の、ポートフォリオ・シミュレーションに要る部分。"""

    ticker_id: int
    base_date: str  # ISO
    probability: float
    realized_return: float
    sector: str | None = None
    cost_bps: float = 0.0


def _select_basket(
    observations: list[SimObservation],
    max_positions: int,
    per_position_cap: float,
    sector_cap: float,
) -> list[PortfolioPosition]:
    """1評価日ぶんの等金額バスケット。確率降順に、セクター上限を守って N 銘柄まで。"""
    ordered = sorted(observations, key=lambda o: (-o.probability, o.ticker_id))
    max_per_sector = max(1, math.floor(max_positions * sector_cap))
    sector_counts: dict[str | None, int] = {}
    picked: list[SimObservation] = []
    for obs in ordered:
        if len(picked) >= max_positions:
            break
        if obs.sector is not None and sector_counts.get(obs.sector, 0) >= max_per_sector:
            continue
        sector_counts[obs.sector] = sector_counts.get(obs.sector, 0) + 1
        picked.append(obs)
    if not picked:
        return []
    # 等金額。per_position_cap を超えないよう名目上限を課す(N が小さいと効く)。
    weight = min(1.0 / len(picked), per_position_cap)
    return [
        PortfolioPosition(
            ticker_id=o.ticker_id,
            base_date=o.base_date,
            sector=o.sector,
            weight=weight,
            realized_return=o.realized_return,
            cost_bps=o.cost_bps,
        )
        for o in picked
    ]


def _basket_return(basket: list[PortfolioPosition], *, net_of_cost: bool) -> float:
    """バスケットの保有期間リターン(等金額加重)。`net_of_cost` なら往復コストを引く。"""
    if not basket:
        return 0.0
    total_weight = sum(p.weight for p in basket)
    if total_weight <= 0:
        return 0.0
    acc = 0.0
    for p in basket:
        r = p.realized_return - (p.cost_bps / 10_000 if net_of_cost else 0.0)
        acc += (p.weight / total_weight) * r
    return acc


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 1.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def _turnover(baskets: list[list[PortfolioPosition]]) -> float:
    if len(baskets) < 2:
        return 0.0
    changes: list[float] = []
    for prev, cur in zip(baskets, baskets[1:]):
        prev_ids = {p.ticker_id for p in prev}
        cur_ids = {p.ticker_id for p in cur}
        if not cur_ids:
            continue
        changes.append(len(cur_ids - prev_ids) / len(cur_ids))
    return statistics.fmean(changes) if changes else 0.0


def simulate_portfolio(
    observations: list[SimObservation],
    *,
    horizon_years: float,
    max_positions: int,
    per_position_cap: float,
    sector_cap: float,
    benchmark_returns: dict[str, dict[str, float]] | None = None,
    net_of_cost: bool = False,
) -> PortfolioBacktest | None:
    """評価日ごとに上位N銘柄を等金額で建てる運用を模擬する(D-4)。

    `benchmark_returns` は {base_date(ISO): {benchmark_symbol: そのホライズンでの
    リターン}}。非重複トランシェ(保有期間が重ならない評価日)で資産曲線を作り、
    CAGR・最大ドローダウン・指数超過CAGRを出す。
    """
    if not observations or horizon_years <= 0:
        return None
    by_date: dict[str, list[SimObservation]] = {}
    for obs in observations:
        by_date.setdefault(obs.base_date, []).append(obs)
    dates = sorted(by_date)
    if not dates:
        return None

    baskets_by_date = {d: _select_basket(by_date[d], max_positions, per_position_cap, sector_cap) for d in dates}
    all_baskets = [baskets_by_date[d] for d in dates]

    # 非重複トランシェ:ホライズン(年)を暦日に直し、それ以上離れた評価日だけ採る。
    # 非重複の判定は暦日で行うが、月末営業日・うるう年で±数日ぶれるので1週間の
    # 許容を持たせる(`runner._MAX_*_LOOKAHEAD_DAYS` と同じ考え方)。
    min_gap_days = horizon_years * 365.25 - 7
    import datetime as _dt

    parsed = [(d, _dt.date.fromisoformat(d)) for d in dates]
    tranche_dates: list[str] = []
    last_dt: _dt.date | None = None
    for d, dt in parsed:
        if last_dt is None or (dt - last_dt).days >= min_gap_days:
            tranche_dates.append(d)
            last_dt = dt

    period_returns = [_basket_return(baskets_by_date[d], net_of_cost=net_of_cost) for d in tranche_dates]
    gross_returns = [_basket_return(baskets_by_date[d], net_of_cost=False) for d in tranche_dates]

    equity = [1.0]
    for r in period_returns:
        equity.append(equity[-1] * (1.0 + r))
    equity_curve: list[tuple[str, float]] = [(tranche_dates[0], 1.0)] if tranche_dates else []
    for d, value in zip(tranche_dates, equity[1:]):
        equity_curve.append((d, value))

    n = len(period_returns)
    total_years = n * horizon_years if n else 0.0
    cumulative = equity[-1]
    cagr = (cumulative ** (1.0 / total_years) - 1.0) if total_years > 0 and cumulative > 0 else 0.0
    gross_cumulative = 1.0
    for r in gross_returns:
        gross_cumulative *= 1.0 + r
    gross_cagr = (
        gross_cumulative ** (1.0 / total_years) - 1.0 if total_years > 0 and gross_cumulative > 0 else 0.0
    )

    volatility = (
        statistics.stdev(period_returns) / math.sqrt(horizon_years)
        if len(period_returns) >= 2 and horizon_years > 0
        else 0.0
    )

    benchmark_cagr: dict[str, float] = {}
    excess_cagr: dict[str, float] = {}
    win_rate: dict[str, float] = {}
    if benchmark_returns:
        symbols = sorted({s for by_sym in benchmark_returns.values() for s in by_sym})
        for symbol in symbols:
            tranche_bench = [
                benchmark_returns.get(d, {}).get(symbol)
                for d in tranche_dates
                if benchmark_returns.get(d, {}).get(symbol) is not None
            ]
            if len(tranche_bench) == n and n > 0:
                bench_cum = 1.0
                for r in tranche_bench:
                    bench_cum *= 1.0 + r
                b_cagr = bench_cum ** (1.0 / total_years) - 1.0 if bench_cum > 0 else -1.0
                benchmark_cagr[symbol] = b_cagr
                excess_cagr[symbol] = cagr - b_cagr
            # 勝率は全評価日(重複含む)で見る——検出力の弱いCAGRより本数が多い。
            wins = comparable = 0
            for d in dates:
                b = benchmark_returns.get(d, {}).get(symbol)
                if b is None:
                    continue
                comparable += 1
                if _basket_return(baskets_by_date[d], net_of_cost=net_of_cost) > b:
                    wins += 1
            if comparable:
                win_rate[symbol] = wins / comparable

    cost_drag = (gross_cagr - cagr) if net_of_cost else 0.0
    overlapping_mean = statistics.fmean(
        _basket_return(baskets_by_date[d], net_of_cost=net_of_cost) for d in dates
    )

    return PortfolioBacktest(
        holdings_per_rebalance=max(len(b) for b in all_baskets) if all_baskets else 0,
        rebalance_dates=dates,
        non_overlapping_tranche_count=n,
        equity_curve=equity_curve,
        cagr=cagr,
        max_drawdown=_max_drawdown(equity),
        volatility=volatility,
        benchmark_cagr=benchmark_cagr,
        excess_cagr=excess_cagr,
        win_rate_vs_benchmark=win_rate,
        turnover=_turnover(all_baskets),
        realized_cost_drag=cost_drag,
        mean_overlapping_period_return=overlapping_mean,
    )
