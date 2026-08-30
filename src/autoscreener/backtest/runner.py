"""擬似バックテスト実行ジョブ(27.8)。

過去の各評価日 T について、

1. `scoring/point_in_time.py` で **T 時点に開示済みだったデータだけ** から
   `MoicInputs` を再構成し、
2. 除外ゲートも T 時点の値で判定し、
3. `scoring/moic.py`(ライブと**同一の関数**)で P(MOIC >= 10) を算出し、
4. T の翌営業日始値で建てて T+ホライズン の終値で決済した実現リターンを付き合わせる

ことで、14.2のKPI(デシル単調性・リフト倍率・破綻回避率・較正誤差)を測る。

**なぜこれが必要か**:14.3は「ポイントインタイム財務が取れない以上バックテストは
不可能。前方検証を待つしかない」と結論し、その結果 `forward_returns` が0行の
まま、8つのサブスコア重みがすべて推測値で運用され続けていた(26.4が自認)。
7年のフィードバックループを持つシステムは、事実上検証されないシステムである。
本ジョブは限界つきでもフィードバックを**今日**得るためにある。

限界(リステートメントの先読み・開示ラグの近似・期数の制約)は
`scoring/point_in_time.py` のdocstringに列挙してある。結果を読むときは必ず
そちらも参照すること。
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field, replace

from sqlalchemy import text
from sqlalchemy.orm import Session

from autoscreener.backtest.metrics import (
    BacktestMetrics,
    Observation,
    compute_metrics,
    cost_adjusted_metrics,
    on_pace_threshold,
    scale_probability_to_horizon,
)
from autoscreener.backtest.baselines import BASELINES, baseline_metrics
from autoscreener.backtest.portfolio_sim import SimObservation, simulate_portfolio
from autoscreener.backtest.stratify import stratify_kpis
from autoscreener.scoring.calibration import CalibrationMap, fit_calibration
from autoscreener.scoring.portfolio import estimate_asset_correlation
from autoscreener.config import (
    ExecutionConfig,
    PortfolioConfig,
    ScoringConfig,
    UniverseCeilings,
    UniverseConfig,
    load_execution_config,
    load_portfolio_config,
    load_scoring_config,
    load_universe_config,
)
from autoscreener.screening.trading_cost import corwin_schultz_spread, round_trip_cost_bps
from autoscreener.db.models import BacktestRun, Ticker
from autoscreener.db.session import session_scope
from autoscreener.scoring.engine import config_hash
from autoscreener.scoring.elasticity import ElasticityEstimate, estimate_growth_elasticity
from autoscreener.scoring.moic import (
    MoicInputs,
    base_initial_growth,
    build_cross_section,
    compute_moic,
    initial_growth_ceiling,
    raw_initial_growth,
)
from autoscreener.scoring.point_in_time import (
    build_gate_input,
    build_moic_inputs,
    build_point_in_time_statements,
)
from autoscreener.screening.exclusion_gates import (
    DILUTION_3Y_CAGR_CEILING,
    evaluate_gates,
    latest_period_value,
)

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 365
DEFAULT_REBALANCE_INTERVAL_DAYS = 91  # 四半期ごと

# 建玉・決済の価格を探すときの許容日数(`forward_validation` と同じ考え方)
_MAX_ENTRY_LOOKAHEAD_DAYS = 7
_MAX_EXIT_LOOKAHEAD_DAYS = 10

# 流動性ゲートに使う出来高の窓(`apply_gates` と同じ90営業日相当)
_LIQUIDITY_WINDOW_DAYS = 130  # 暦日。おおよそ90営業日

# 希薄化の日次観測は月次にサンプリングする。3年×5,000銘柄の全行を保持すると
# メモリに乗らないうえ、CAGRの算出に日次の解像度は必要ない。
_SHARE_SAMPLE_INTERVAL_DAYS = 30

# 28.3のナウキャストが見る終値も同じ理由で月次サンプリングする。
# `annualized_log_momentum` は実測の経過年数で年率換算するため、日次(ライブ)と
# 月次(バックテスト)でほぼ同じ値になる。評価日当日の終値だけは `_load_slice` の
# 正確な値を後から足し込むので、時価総額の精度は落ちない。
_PRICE_SAMPLE_INTERVAL_DAYS = 30

# 28.12:資産相関の推定に使う評価日の最小観測数
_MIN_DATES_FOR_CORRELATION = 100


@dataclass(frozen=True)
class RebalanceSlice:
    """1評価日分の、価格から引ける値。"""

    as_of: datetime.date
    close: dict[int, float]
    shares: dict[int, float]
    entry_open: dict[int, float]
    exit_close: dict[int, float]
    # 保有期間 (as_of, target_date] のうち**最後に観測された終値**。上場廃止で
    # 決済価格が取れない銘柄をここで清算する(27.11)。`close` は評価日時点の
    # 株価(=建玉のときの値)なので、これを廃止時の清算価格に使ってはいけない。
    final_close: dict[int, float]
    median_dollar_volume: dict[int, float]
    last_price_date: dict[int, datetime.date]
    # D-5(defect_and_edge_audit_2026-08-28.md):`as_of` 直近の日次 (high, low) 系列
    # (古い→新しい)。Corwin–Schultz 実効スプレッド推定に使う。
    recent_hl_bars: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    # D-11:保有期間 (as_of, target] に落ちた1株あたり配当の合計。総リターン算出用。
    dividends_in_holding: dict[int, float] = field(default_factory=dict)


def _load_payloads(session: Session) -> dict[int, tuple[dict, str | None]]:
    """銘柄ごとの最新 raw_snapshot(payload, セクター)。

    財務諸表は `build_point_in_time_statements` が評価日ごとに切るので、
    ここでは最新の1件を読むだけでよい(過去のスナップショットは不要)。
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id) ticker_id, payload
            FROM raw_snapshots
            ORDER BY ticker_id, snapshot_date DESC
            """
        )
    ).all()
    payloads: dict[int, tuple[dict, str | None]] = {}
    for ticker_id, payload in rows:
        info = payload.get("info") or {}
        payloads[ticker_id] = (payload, info.get("sector"))
    return payloads


def _load_share_observations(session: Session) -> dict[int, list[tuple[datetime.date, float | None]]]:
    """発行済株式数の月次サンプル(希薄化CAGR用)。"""
    rows = session.execute(
        text(
            """
            SELECT ticker_id, trade_date, shares_outstanding
            FROM (
                SELECT ticker_id, trade_date, shares_outstanding,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker_id, (trade_date - DATE '2000-01-01') / :interval
                           ORDER BY trade_date
                       ) AS rn
                FROM price_snapshots
                WHERE shares_outstanding IS NOT NULL
            ) sampled
            WHERE rn = 1
            ORDER BY ticker_id, trade_date
            """
        ),
        {"interval": _SHARE_SAMPLE_INTERVAL_DAYS},
    ).all()
    observations: dict[int, list[tuple[datetime.date, float | None]]] = {}
    for ticker_id, trade_date, shares in rows:
        observations.setdefault(ticker_id, []).append((trade_date, float(shares)))
    return observations


def _load_price_observations(session: Session) -> dict[int, list[tuple[datetime.date, float]]]:
    """終値の月次サンプル(28.3のナウキャスト用)。

    `_load_share_observations` と同じ理由で月次に落とす。評価日当日の終値は
    `_evaluate_one_date` が `_load_slice` の値を末尾に足すため、時価総額に
    使われる価格は常に正確な「`as_of` 以前で最後の終値」である。
    """
    rows = session.execute(
        text(
            """
            SELECT ticker_id, trade_date, close
            FROM (
                SELECT ticker_id, trade_date, close,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker_id, (trade_date - DATE '2000-01-01') / :interval
                           ORDER BY trade_date
                       ) AS rn
                FROM price_snapshots
                WHERE close IS NOT NULL
            ) sampled
            WHERE rn = 1
            ORDER BY ticker_id, trade_date
            """
        ),
        {"interval": _PRICE_SAMPLE_INTERVAL_DAYS},
    ).all()
    observations: dict[int, list[tuple[datetime.date, float]]] = {}
    for ticker_id, trade_date, close in rows:
        observations.setdefault(ticker_id, []).append((trade_date, float(close)))
    return observations


def _load_slice(session: Session, as_of: datetime.date, horizon_days: int) -> RebalanceSlice:
    """評価日 `as_of` に必要な価格系の値をまとめて引く。"""
    target_date = as_of + datetime.timedelta(days=horizon_days)

    close_rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id) ticker_id, close, shares_outstanding
            FROM price_snapshots
            WHERE trade_date <= :as_of AND close IS NOT NULL
            ORDER BY ticker_id, trade_date DESC
            """
        ),
        {"as_of": as_of},
    ).all()
    close = {r[0]: float(r[1]) for r in close_rows}
    shares = {r[0]: float(r[2]) for r in close_rows if r[2] is not None}

    entry_rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id) ticker_id, open
            FROM price_snapshots
            WHERE trade_date > :as_of AND trade_date <= :limit AND open IS NOT NULL
            ORDER BY ticker_id, trade_date ASC
            """
        ),
        {"as_of": as_of, "limit": as_of + datetime.timedelta(days=_MAX_ENTRY_LOOKAHEAD_DAYS)},
    ).all()

    exit_rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id) ticker_id, close
            FROM price_snapshots
            WHERE trade_date >= :target AND trade_date <= :limit AND close IS NOT NULL
            ORDER BY ticker_id, trade_date ASC
            """
        ),
        {"target": target_date, "limit": target_date + datetime.timedelta(days=_MAX_EXIT_LOOKAHEAD_DAYS)},
    ).all()

    # 上場廃止の清算価格(27.11)。保有期間の中で最後に観測された終値であって、
    # 評価日時点の株価ではない。`forward_validation._settle_delisted` と同じ定義。
    final_rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (ticker_id) ticker_id, close
            FROM price_snapshots
            WHERE trade_date > :as_of AND trade_date <= :target AND close IS NOT NULL
            ORDER BY ticker_id, trade_date DESC
            """
        ),
        {"as_of": as_of, "target": target_date},
    ).all()

    volume_rows = session.execute(
        text(
            """
            SELECT ticker_id, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY close * volume)
            FROM price_snapshots
            WHERE trade_date <= :as_of AND trade_date > :window_start
              AND close IS NOT NULL AND volume IS NOT NULL
            GROUP BY ticker_id
            """
        ),
        {"as_of": as_of, "window_start": as_of - datetime.timedelta(days=_LIQUIDITY_WINDOW_DAYS)},
    ).all()

    # 上場廃止判定のための「最後に価格が観測された日」(27.11と同じ考え方)
    last_date_rows = session.execute(
        text("SELECT ticker_id, MAX(trade_date) FROM price_snapshots GROUP BY ticker_id")
    ).all()

    # D-5:直近の日次 (high, low)。Corwin–Schultz 実効スプレッド推定に使う。
    # 21営業日ぶんあれば足りるので暦日35日で切る(2日ペアの推定量を平均する)。
    hl_rows = session.execute(
        text(
            """
            SELECT ticker_id, high, low
            FROM price_snapshots
            WHERE trade_date <= :as_of AND trade_date > :cs_start
              AND high IS NOT NULL AND low IS NOT NULL
            ORDER BY ticker_id, trade_date ASC
            """
        ),
        {"as_of": as_of, "cs_start": as_of - datetime.timedelta(days=35)},
    ).all()
    recent_hl_bars: dict[int, list[tuple[float, float]]] = {}
    for ticker_id, high, low in hl_rows:
        recent_hl_bars.setdefault(ticker_id, []).append((float(high), float(low)))

    # D-11:保有期間中に落ちた配当の合計(総リターン算出用)。
    dividend_rows = session.execute(
        text(
            """
            SELECT ticker_id, COALESCE(SUM(dividend), 0)
            FROM price_snapshots
            WHERE trade_date > :as_of AND trade_date <= :target AND dividend IS NOT NULL
            GROUP BY ticker_id
            """
        ),
        {"as_of": as_of, "target": target_date},
    ).all()
    dividends_in_holding = {r[0]: float(r[1]) for r in dividend_rows}

    return RebalanceSlice(
        as_of=as_of,
        close=close,
        shares=shares,
        entry_open={r[0]: float(r[1]) for r in entry_rows},
        exit_close={r[0]: float(r[1]) for r in exit_rows},
        final_close={r[0]: float(r[1]) for r in final_rows},
        median_dollar_volume={r[0]: float(r[1]) for r in volume_rows if r[1] is not None},
        last_price_date={r[0]: r[1] for r in last_date_rows},
        recent_hl_bars=recent_hl_bars,
        dividends_in_holding=dividends_in_holding,
    )


def _benchmark_returns(
    session: Session, dates: list[datetime.date], horizon_days: int
) -> dict[str, dict[str, float]]:
    """D-4:各評価日における各ベンチマークETFのホライズン・リターン。

    {base_date(ISO): {symbol: exit/entry - 1}}。建て値は評価日以前の最後の終値、
    決済値は目標日以降の最初の終値(`_MAX_*_LOOKAHEAD_DAYS` と同じ許容)。
    """
    benchmarks = session.query(Ticker.id, Ticker.symbol).filter(Ticker.is_benchmark.is_(True)).all()
    if not benchmarks:
        return {}
    out: dict[str, dict[str, float]] = {}
    for as_of in dates:
        target = as_of + datetime.timedelta(days=horizon_days)
        for ticker_id, symbol in benchmarks:
            entry = session.execute(
                text(
                    "SELECT close FROM price_snapshots WHERE ticker_id = :tid "
                    "AND trade_date <= :as_of AND close IS NOT NULL "
                    "ORDER BY trade_date DESC LIMIT 1"
                ),
                {"tid": ticker_id, "as_of": as_of},
            ).scalar()
            exit_price = session.execute(
                text(
                    "SELECT close FROM price_snapshots WHERE ticker_id = :tid "
                    "AND trade_date >= :target AND trade_date <= :limit AND close IS NOT NULL "
                    "ORDER BY trade_date ASC LIMIT 1"
                ),
                {
                    "tid": ticker_id,
                    "target": target,
                    "limit": target + datetime.timedelta(days=_MAX_EXIT_LOOKAHEAD_DAYS),
                },
            ).scalar()
            if entry and exit_price and float(entry) > 0:
                out.setdefault(as_of.isoformat(), {})[symbol] = float(exit_price) / float(entry) - 1.0
    return out


def _passes_point_in_time_gate(
    inputs: MoicInputs,
    payload: dict,
    as_of: datetime.date,
    price: float,
    median_dollar_volume: float | None,
    universe_config: UniverseConfig,
    scoring_config: ScoringConfig,
    ceilings: UniverseCeilings,
) -> bool:
    """D-10(defect_and_edge_audit_2026-08-28.md):ライブと**同一の** `evaluate_gates`
    を、ポイントインタイム値で通す。

    以前はここにライブと別物のゲートを実装しており、`cash_runway_floor`
    (6四半期未満で除外)がライブでだけ効いていた——σ と health_index が最も
    効いている脆弱・高ボラ銘柄群を、KPIを測った母集団からは削らずライブでだけ
    削っていた。`point_in_time.build_gate_input` がキャッシュランウェイ・
    上場後期数を四半期データから(取れなければ年次から)再構成する。

    規模の上限だけは目標倍率の関数(29章)なので、`evaluate_gates` の
    materialize 上限より先に**検証している目標の上限**で切る。
    """
    if inputs.market_cap >= ceilings.market_cap_usd:
        return False
    if inputs.revenue_latest >= ceilings.revenue_usd:
        return False
    gate_input = build_gate_input(
        payload,
        as_of,
        inputs,
        price,
        median_dollar_volume,
        scoring_config.requirements.min_annual_revenue_periods,
    )
    return evaluate_gates(gate_input, universe_config).passed


def _passes_legacy_gate(
    inputs: MoicInputs,
    payload: dict,
    as_of: datetime.date,
    price: float,
    median_dollar_volume: float | None,
    universe_config: UniverseConfig,
    scoring_config: ScoringConfig,
    ceilings: UniverseCeilings,
) -> bool:
    """D-10:旧「バックテスト専用」ゲート(年次期数のみ・キャッシュランウェイ無視)。

    現在は使わない。`gate_parity`(ライブ相当ゲート通過数 / 旧ゲート通過数)を
    評価日ごとに出すためだけに残す——差分がどこで効いているかを可視化する。
    """
    if inputs.market_cap >= ceilings.market_cap_usd:
        return False
    if inputs.revenue_latest >= ceilings.revenue_usd:
        return False
    if price < universe_config.min_price_usd:
        return False
    if inputs.sector is None or inputs.sector in universe_config.excluded_sectors:
        return False
    if (
        median_dollar_volume is not None
        and median_dollar_volume < universe_config.min_daily_dollar_volume_usd
    ):
        return False
    if inputs.dilution_cagr is not None and inputs.dilution_cagr > DILUTION_3Y_CAGR_CEILING:
        return False
    statements = build_point_in_time_statements(payload, as_of)
    equity = latest_period_value(statements.balance_sheet.get("Stockholders Equity"))
    if equity is not None and equity < 0:
        return False
    return (
        len(statements.visible_period_ends)
        >= scoring_config.requirements.min_annual_revenue_periods
    )


def _realized_return(
    ticker_id: int, price_slice: RebalanceSlice, target_date: datetime.date
) -> tuple[float, str] | None:
    """建玉→決済の実現リターンと決済区分。建てられなければ None。

    決済価格が取れない場合、**最後に観測された価格で決済する**(27.11)。
    上場廃止銘柄を単に捨てると、負けの極端値だけが標本から消え、KPIが
    実態より良く出る——`forward_validation._settle_delisted` と同じ理由。
    """
    entry = price_slice.entry_open.get(ticker_id)
    if entry is None or entry <= 0:
        return None

    # D-11(defect_and_edge_audit_2026-08-28.md):価格リターンではなく総リターン。
    # 保有期間に落ちた配当を分子に足す。配当を無視すると、成熟した配当銘柄が
    # 混じるユニバースの基準率(リフトの分母)が系統的に低く出る。
    dividends = price_slice.dividends_in_holding.get(ticker_id, 0.0)

    exit_price = price_slice.exit_close.get(ticker_id)
    if exit_price is not None:
        return (exit_price + dividends) / entry - 1, "market"

    last_date = price_slice.last_price_date.get(ticker_id)
    if last_date is not None and last_date < target_date - datetime.timedelta(days=_MAX_EXIT_LOOKAHEAD_DAYS):
        # 目標日より十分前で価格が途切れている = 実質的に取引が終わっている。
        # 廃止直前の価格(保有期間の中で最後に観測された終値)で決済する。
        #
        # **ここで `price_slice.close` を使ってはいけない。** それは評価日時点の
        # 終値=建玉した瞬間の株価なので、実現リターンがほぼ0%になる。上場廃止は
        # −90%〜−100%と強く相関する事象であり、それを「±0%で決済した」ことに
        # すると、27.11が取り除こうとした生存バイアスが**別の形でそのまま残る**
        # (負けの極端値が消える代わりに、中立値に置き換わるだけ)。
        final_close = price_slice.final_close.get(ticker_id)
        if final_close is not None:
            return (final_close + dividends) / entry - 1, "delisted"
        # B-2(defect_and_edge_audit_2026-08-28.md D-1):価格が全く取れなかった
        # 廃止銘柄。−100% で決め打ちせず別区分にして、KPIを「含めた場合/除いた
        # 場合」の両方で出せるようにする(片方に決め打ちすると隠れバイアスになる)。
        return -1.0, "delisted_unpriced"
    return None


def rebalance_dates(
    first_price_date: datetime.date,
    last_price_date: datetime.date,
    horizon_days: int,
    interval_days: int = DEFAULT_REBALANCE_INTERVAL_DAYS,
) -> list[datetime.date]:
    """評価日の一覧。最後の評価日はホライズンが満期を迎えられる日までに限る。"""
    latest = last_price_date - datetime.timedelta(days=horizon_days)
    if latest < first_price_date:
        return []
    dates: list[datetime.date] = []
    current = first_price_date
    while current <= latest:
        dates.append(current)
        current += datetime.timedelta(days=interval_days)
    return dates


def _evaluate_one_date(
    session: Session,
    as_of: datetime.date,
    horizon_days: int,
    payloads: dict[int, tuple[dict, str | None]],
    share_observations: dict[int, list[tuple[datetime.date, float | None]]],
    price_observations: dict[int, list[tuple[datetime.date, float]]],
    scoring_config: ScoringConfig,
    universe_config: UniverseConfig,
    ceilings: UniverseCeilings,
    execution_config: ExecutionConfig,
    portfolio_config: PortfolioConfig,
    parity: dict[str, int] | None = None,
) -> list[Observation]:
    price_slice = _load_slice(session, as_of, horizon_days)
    target_date = as_of + datetime.timedelta(days=horizon_days)
    # D-5:名目建玉サイズ(往復コストの平方根則インパクトに使う)。
    nominal_position_usd = portfolio_config.portfolio_value_usd * portfolio_config.per_position_cap

    inputs_by_ticker: dict[int, MoicInputs] = {}
    legacy_pass = 0  # D-10:旧「バックテスト専用」ゲートの通過数(gate_parity 用)

    for ticker_id, (payload, sector) in payloads.items():
        price = price_slice.close.get(ticker_id)
        if price is None:
            continue
        # 月次サンプルの末尾に評価日当日の正確な終値を足す。時価総額はこの
        # 終値から作られ、モメンタムは月次サンプルの窓から作られる。
        prices = [*price_observations.get(ticker_id, []), (as_of, price)]
        inputs = build_moic_inputs(
            payload=payload,
            share_observations=share_observations.get(ticker_id, []),
            price_observations=prices,
            as_of=as_of,
            sector=sector,
        )
        if inputs is None:
            continue
        median_dv = price_slice.median_dollar_volume.get(ticker_id)
        if _passes_legacy_gate(
            inputs, payload, as_of, price, median_dv, universe_config, scoring_config, ceilings
        ):
            legacy_pass += 1
        if not _passes_point_in_time_gate(
            inputs, payload, as_of, price, median_dv, universe_config, scoring_config, ceilings
        ):
            continue
        inputs_by_ticker[ticker_id] = inputs

    if parity is not None:
        parity["live_pass"] = parity.get("live_pass", 0) + len(inputs_by_ticker)
        parity["legacy_pass"] = parity.get("legacy_pass", 0) + legacy_pass

    if not inputs_by_ticker:
        return []

    # 28.5:ナウキャストの基準線と σ の縮小中心を、その評価日のゲート通過集合から
    # 作る。ライブの `engine.cross_section_for` と同一の関数を通す。
    cross_section = build_cross_section(list(inputs_by_ticker.values()), scoring_config)

    observations: list[Observation] = []
    for ticker_id, inputs in inputs_by_ticker.items():
        result = compute_moic(inputs, cross_section, scoring_config)
        if result is None:
            continue
        realized = _realized_return(ticker_id, price_slice, target_date)
        if realized is None:
            continue
        realized_return, settlement = realized
        spread = corwin_schultz_spread(price_slice.recent_hl_bars.get(ticker_id, []))
        cost_bps = round_trip_cost_bps(
            spread,
            nominal_position_usd,
            price_slice.median_dollar_volume.get(ticker_id),
            execution_config.impact_coefficient,
            commission_bps=execution_config.commission_bps,
            min_half_spread_bps=execution_config.min_half_spread_bps,
        )
        observations.append(
            Observation(
                ticker_id=ticker_id,
                base_date=as_of.isoformat(),
                probability=result.probability,
                log_moic_mu=result.log_moic_mu,
                log_moic_sigma=result.log_moic_sigma,
                realized_return=realized_return,
                settlement=settlement,
                expected_moic=result.expected_moic,
                market_cap=inputs.market_cap,
                sector=inputs.sector,
                growth_nowcast_adjustment=result.growth_nowcast_adjustment,
                cost_bps=cost_bps,
                # D-8:単純ベースラインのスコア(同一観測で v4 と比較する)。
                baseline_scores={
                    name: fn(inputs, cross_section, scoring_config)
                    for name, fn in BASELINES.items()
                },
            )
        )
    return observations


def collect_backtest_observations(
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    interval_days: int = DEFAULT_REBALANCE_INTERVAL_DAYS,
    scoring_config: ScoringConfig | None = None,
    universe_config: UniverseConfig | None = None,
) -> list[Observation]:
    """擬似バックテストの観測を集めるところまで(KPI算出・保存はしない)。

    A-4(defect_and_edge_audit_2026-08-28.md D-2)の `compare-configs` が、同じ
    価格・財務データに対して2つの `config/scoring.yaml` でKPIを出して比較する
    ために使う。**設定ごとに観測を作り直す必要がある**——`MoicInputs` は共通でも
    断面統計(σの縮小中心・ナウキャスト基準線)・生存ハザード・κ が設定で変わる。
    """
    scoring_config = scoring_config or load_scoring_config()
    universe_config = universe_config or load_universe_config()
    execution_config = load_execution_config()
    portfolio_config = load_portfolio_config()
    ceilings = universe_config.ceilings_for_target(scoring_config.target_moic)

    with session_scope() as session:
        bounds = session.execute(
            text("SELECT MIN(trade_date), MAX(trade_date) FROM price_snapshots")
        ).one()
        first_price_date, last_price_date = bounds
        if first_price_date is None:
            return []
        dates = rebalance_dates(first_price_date, last_price_date, horizon_days, interval_days)
        if not dates:
            return []

        payloads = _load_payloads(session)
        share_observations = _load_share_observations(session)
        price_observations = _load_price_observations(session)

        observations: list[Observation] = []
        for as_of in dates:
            batch = _evaluate_one_date(
                session,
                as_of,
                horizon_days,
                payloads,
                share_observations,
                price_observations,
                scoring_config,
                universe_config,
                ceilings,
                execution_config,
                portfolio_config,
            )
            logger.info("rebalance %s: %d observations", as_of, len(batch))
            observations.extend(batch)
        return observations


def run_backtest(
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    interval_days: int = DEFAULT_REBALANCE_INTERVAL_DAYS,
    scoring_config: ScoringConfig | None = None,
    universe_config: UniverseConfig | None = None,
    persist: bool = True,
    bootstrap_resamples: int = 300,
) -> BacktestMetrics:
    """擬似バックテストを実行し、KPIを返す(`persist=True` なら `backtest_runs` に保存)。"""
    scoring_config = scoring_config or load_scoring_config()
    universe_config = universe_config or load_universe_config()
    execution_config = load_execution_config()
    portfolio_config = load_portfolio_config()
    # 29章:規模の上限は目標倍率の関数。**検証している目標の上限で母集団を切る**。
    # 既定(10倍)なら 3.5B/3B となり、29章以前と同じ母集団を再現する。
    ceilings = universe_config.ceilings_for_target(scoring_config.target_moic)
    # D-2:評価日の間隔がホライズン以上なら保有期間が重ならない=非重複モード。
    non_overlapping = interval_days >= horizon_days

    with session_scope() as session:
        bounds = session.execute(text("SELECT MIN(trade_date), MAX(trade_date) FROM price_snapshots")).one()
        first_price_date, last_price_date = bounds
        if first_price_date is None:
            logger.error("price_snapshots is empty — run backfill-history before backtesting")
            return compute_metrics([], scoring_config.target_moic, 0.0, scoring_config.horizon_years)

        dates = rebalance_dates(first_price_date, last_price_date, horizon_days, interval_days)
        if not dates:
            logger.error(
                "price history spans %s..%s which is shorter than the %d-day horizon",
                first_price_date,
                last_price_date,
                horizon_days,
            )
            return compute_metrics([], scoring_config.target_moic, 0.0, scoring_config.horizon_years)

        logger.info("loading payloads, share observations and price observations")
        payloads = _load_payloads(session)
        share_observations = _load_share_observations(session)
        price_observations = _load_price_observations(session)

        observations: list[Observation] = []
        gate_parity: dict[str, int] = {}
        for as_of in dates:
            batch = _evaluate_one_date(
                session,
                as_of,
                horizon_days,
                payloads,
                share_observations,
                price_observations,
                scoring_config,
                universe_config,
                ceilings,
                execution_config,
                portfolio_config,
                parity=gate_parity,
            )
            logger.info("rebalance %s: %d observations", as_of, len(batch))
            observations.extend(batch)

        metrics = compute_metrics(
            observations,
            target_moic=scoring_config.target_moic,
            horizon_years=horizon_days / 365.25,
            model_horizon_years=scoring_config.horizon_years,
            nowcast_cap=scoring_config.growth.nowcast_cap,
            bootstrap_resamples=bootstrap_resamples,
            non_overlapping=non_overlapping,
            kpi_acceptance=scoring_config.kpi_acceptance,
        )
        # D-5:コスト後の主要KPIを別立てで持たせる。
        after_cost = cost_adjusted_metrics(
            observations,
            scoring_config.target_moic,
            horizon_days / 365.25,
            scoring_config.horizon_years,
        )
        metrics = replace(metrics, after_cost=after_cost)

        # D-4:ポートフォリオ・シミュレーション(指数超過CAGR・最大ドローダウン)。
        benchmark_returns = _benchmark_returns(session, dates, horizon_days)
        sim = simulate_portfolio(
            [
                SimObservation(
                    ticker_id=o.ticker_id,
                    base_date=o.base_date,
                    probability=o.probability,
                    realized_return=o.realized_return,
                    sector=o.sector,
                    cost_bps=o.cost_bps,
                )
                for o in observations
            ],
            horizon_years=horizon_days / 365.25,
            max_positions=portfolio_config.max_positions,
            per_position_cap=portfolio_config.per_position_cap,
            sector_cap=portfolio_config.sector_cap,
            benchmark_returns=benchmark_returns,
            net_of_cost=True,
        )
        metrics = replace(metrics, portfolio=sim.as_dict() if sim else None)

        # D-10:ライブ相当ゲート通過数 / 旧ゲート通過数。
        gate_parity_summary = dict(gate_parity)
        if gate_parity.get("legacy_pass"):
            gate_parity_summary["ratio"] = gate_parity["live_pass"] / gate_parity["legacy_pass"]
        metrics = replace(metrics, gate_parity=gate_parity_summary or None)

        # D-8:単純ベースラインとの比較(同一観測)。
        baselines = baseline_metrics(
            observations,
            scoring_config.target_moic,
            horizon_days / 365.25,
            scoring_config.horizon_years,
        )
        metrics = replace(metrics, baselines=baselines or None)

        # C-5(I-4):KPIの層別。浮動株比率(EntityPublicFloat)が MoicInputs に
        # 入るまでは市場規模で層別する——リフトがどの規模帯で立っているかは、
        # 「プロと同じ土俵か / 構造優位の帯か」の最初の手がかりになる。
        strat = stratify_kpis(
            observations,
            key=lambda o: o.market_cap if o.market_cap and o.market_cap > 0 else None,
            target_moic=scoring_config.target_moic,
            horizon_years=horizon_days / 365.25,
            model_horizon_years=scoring_config.horizon_years,
        )
        metrics = replace(metrics, stratified_kpis={"by_market_cap": strat})

        calibration = fit_calibration_from_observations(observations, horizon_days, scoring_config)
        # 28.12:資産相関の推定には**銘柄数が十分ある評価日だけ**を使う。
        # 価格ヒストリーの先頭付近は可視な年次期数が足りず観測が数十件しか
        # 出ないため(2023-11は21件)、そこの的中率は二項ノイズそのものであり、
        # 混ぜると共通因子の効きを過大に見積もる。
        dense = [d for d in metrics.per_date if d.count >= _MIN_DATES_FOR_CORRELATION]
        correlation = estimate_asset_correlation(
            [d.universe_on_pace_rate for d in dense], [d.count for d in dense]
        )

        if persist:
            session.add(
                BacktestRun(
                    scoring_version=scoring_config.scoring_version,
                    config_hash=config_hash(scoring_config),
                    horizon_days=horizon_days,
                    rebalance_dates=[d.isoformat() for d in dates],
                    observation_count=len(observations),
                    metrics=metrics.as_dict(),
                    config_snapshot=json.loads(scoring_config.model_dump_json()),
                    calibration_map=calibration.to_dict() if calibration else None,
                    asset_correlation=correlation,
                    overlapping=not non_overlapping,
                )
            )

    return metrics


def fit_calibration_from_observations(
    observations: list[Observation], horizon_days: int, scoring_config: ScoringConfig
) -> CalibrationMap | None:
    """観測から較正写像を学習する(28.8)。

    較正の対象は「7年で10倍」ではなく、**このバックテストが実際に観測している
    事象**=「ホライズン h の期間でオンペースに乗ったか」である。7年後の実測は
    原理的に今日存在しないため、そちらは較正しようがない。

    予測側は `scale_probability_to_horizon` でモデルの7年分布を h 年へ引き直した
    値を使う。この引き直し自体が近似であること(成長減衰により初期のドリフトが
    大きいため、短いホライズンでの上昇をやや過小評価する)は較正で吸収される
    ——というより、**較正はまさにその種のずれを実測で吸収するための層**である。
    """
    if not scoring_config.calibration.enabled or not observations:
        return None
    horizon_years = horizon_days / 365.25
    threshold = on_pace_threshold(
        scoring_config.target_moic, horizon_years, scoring_config.horizon_years
    )
    predicted = [
        scale_probability_to_horizon(
            o.log_moic_mu,
            o.log_moic_sigma,
            scoring_config.target_moic,
            horizon_years,
            scoring_config.horizon_years,
        )
        for o in observations
    ]
    outcomes = [1 + o.realized_return >= threshold for o in observations]
    return fit_calibration(
        predicted,
        outcomes,
        horizon_days=horizon_days,
        bins=scoring_config.calibration.bins,
        min_observations=scoring_config.calibration.min_observations,
    )


@dataclass(frozen=True)
class ElasticityCrossSection:
    """1評価日分の κ 推定。E-4(defect_audit_2026-08-27.md)。

    `full` は全銘柄(初期成長率がクランプされた銘柄を含む)での推定で、これが
    従来 `estimate_elasticity_over_history` が返していた値。`unclamped` は
    `raw = min(CAGR, YoY)` が上限・下限に達していない銘柄だけに絞った推定。

    クランプされた銘柄群では、真の成長率が 80% でも 150% でも同じ点
    (max_initial_rate)に潰れて回帰へ入る(打ち切り回帰)。高成長域で説明変数の
    分散が人為的に圧縮されるため、`full` の傾き κ は減衰方向にバイアスがかかり
    うる(regression dilution)。2値が大きく異ならなければ現状の κ 使用は妥当。
    乖離が大きければ、`estimate_elasticity_over_history` の説明変数を
    `raw_initial_growth`(クランプ前)へ切り替えることを検討する。
    """

    full: ElasticityEstimate | None
    unclamped: ElasticityEstimate | None
    clamped_count: int
    total_count: int


def estimate_elasticity_over_history(
    interval_days: int = 60,
    scoring_config: ScoringConfig | None = None,
    universe_config: UniverseConfig | None = None,
) -> list[tuple[datetime.date, ElasticityCrossSection]]:
    """複数の過去断面で、マルチプルの成長弾力性 κ を推定する(28.2)。

    バックテストと同じ経路で「その時点で開示済みだったデータだけ」から
    (成長率, EV/粗利) の組を作り、断面ごとに回帰する。ホライズンを必要と
    しない(将来リターンを見ないので)ため、`rebalance_dates` の制約は外して
    価格ヒストリーの全期間を使える。

    **リターンには一切触れない。** ここで測るのは市場の値づけ構造だけである。

    E-4(2026-08-27):初期成長率がモデルの上限/下限に張り付いた銘柄を含む
    推定(`full`)と、張り付いていない銘柄だけの推定(`unclamped`)の両方を
    返す。CLI がこの2値を並べて出し、乖離の有無で κ の測定方法の妥当性を
    人間が判断する。
    """
    scoring_config = scoring_config or load_scoring_config()
    universe_config = universe_config or load_universe_config()
    # 29章:κ は目標倍率によらず**すべての目標で共有される**構造パラメータなので、
    # 特定の目標の母集団ではなく materialize 済みの母集団そのもので測る
    # (= `market_cap_ceiling_usd` / `revenue_ceiling_usd` をそのまま使う)。
    ceilings = universe_config.ceilings_for_target(universe_config.min_supported_target_moic)

    results: list[tuple[datetime.date, ElasticityCrossSection]] = []
    with session_scope() as session:
        first, last = session.execute(
            text("SELECT MIN(trade_date), MAX(trade_date) FROM price_snapshots")
        ).one()
        if first is None:
            return []

        payloads = _load_payloads(session)
        share_observations = _load_share_observations(session)
        price_observations = _load_price_observations(session)

        as_of = first
        while as_of <= last:
            price_slice = _load_slice(session, as_of, horizon_days=0)
            observations: list[tuple[float, float]] = []
            unclamped_observations: list[tuple[float, float]] = []
            clamped_count = 0
            for ticker_id, (payload, sector) in payloads.items():
                price = price_slice.close.get(ticker_id)
                if price is None:
                    continue
                inputs = build_moic_inputs(
                    payload=payload,
                    share_observations=share_observations.get(ticker_id, []),
                    price_observations=[*price_observations.get(ticker_id, []), (as_of, price)],
                    as_of=as_of,
                    sector=sector,
                )
                if inputs is None:
                    continue
                if not _passes_point_in_time_gate(
                    inputs,
                    payload,
                    as_of,
                    price,
                    price_slice.median_dollar_volume.get(ticker_id),
                    universe_config,
                    scoring_config,
                    ceilings,
                ):
                    continue
                # 弾力性は**財務諸表だけから出した成長率**に対して測る。
                # ナウキャスト補正後の値を使うと、価格を右辺にも左辺にも入れる
                # ことになり(EV/粗利の分子は価格)、相関が自己言及的になる。
                growth = base_initial_growth(inputs, scoring_config)
                enterprise_value = inputs.market_cap + inputs.net_debt
                if growth is None or enterprise_value <= 0 or inputs.gross_profit_latest <= 0:
                    continue
                ratio = enterprise_value / inputs.gross_profit_latest
                observations.append((growth, ratio))

                # E-4:`growth` は _clamp 済みなので、真の成長率が異なる高成長企業も
                # すべて上限の一点に潰れて回帰へ入る(打ち切り回帰 → κ が減衰方向へ
                # バイアス)。クランプ前の生値が上限/下限に達していない銘柄だけを
                # 別集計し、両方の κ を並べて出せるようにする。
                raw_growth = raw_initial_growth(inputs)
                if raw_growth is None:
                    continue
                ceiling = initial_growth_ceiling(inputs, scoring_config)
                floor = scoring_config.growth.min_initial_rate
                if raw_growth >= ceiling or raw_growth <= floor:
                    clamped_count += 1
                else:
                    unclamped_observations.append((raw_growth, ratio))

            estimate = estimate_growth_elasticity(observations)
            unclamped_estimate = estimate_growth_elasticity(unclamped_observations)
            if estimate is not None:
                results.append(
                    (
                        as_of,
                        ElasticityCrossSection(
                            full=estimate,
                            unclamped=unclamped_estimate,
                            clamped_count=clamped_count,
                            total_count=len(observations),
                        ),
                    )
                )
                unclamped_slope = (
                    f"{unclamped_estimate.slope:+.3f}" if unclamped_estimate is not None else "n/a"
                )
                logger.info(
                    "%s: kappa=%+.3f (n=%d) | unclamped kappa=%s (n=%d, clamped=%d)",
                    as_of,
                    estimate.slope,
                    estimate.sample_size,
                    unclamped_slope,
                    unclamped_estimate.sample_size if unclamped_estimate is not None else 0,
                    clamped_count,
                )
            as_of += datetime.timedelta(days=interval_days)

    return results
