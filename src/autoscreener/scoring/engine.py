"""スコアリングエンジン(27章)。

15.2 Step 1(除外ゲート)を通過した銘柄について、`point_in_time.build_moic_inputs`
で入力を組み立て、`moic.compute_moic` で P(MOIC >= 10) を算出して `scores` に
書き込む。

**旧v2からの構造変更**:サブスコアという概念が無くなったため、
「指標のパーセンタイル化 → サブスコア平均 → 加重幾何平均 → カバレッジ補正」
という4段の処理は丸ごと消えた。クロスセクションの集計が必要なのは
**終端マルチプルの回帰先となるセクター中央値**の1箇所だけである。

旧実装にあった二重の縮小推定(`_shrink_toward_neutral` と `coverage_ratio**0.5`)も
削除した。両者は実測で `corr(指標カバレッジ, ln時価総額) = +0.183` を通じて
**成熟した大型銘柄ほど有利**に働いており、10倍余地が最も大きい小型株を系統的に
沈めていた(27.1)。新モデルでは、入力が足りない銘柄は「低スコア」ではなく
「算出不能」として Tier 2 の監視対象へ回す——欠損を減点に読み替えない。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.config import (
    ScoringConfig,
    UniverseConfig,
    load_scoring_config,
    load_universe_config,
)
from autoscreener.dates import business_days_between, utc_today
from autoscreener.backtest.metrics import scale_probability_to_horizon
from autoscreener.db.models import BacktestRun, PriceSnapshot, RawSnapshot, Score, UniverseSnapshot
from autoscreener.scoring.calibration import CalibrationMap
from autoscreener.db.session import session_scope
from autoscreener.scoring.moic import (
    CrossSection,
    MoicInputs,
    MoicResult,
    build_cross_section,
    compute_moic,
)
from autoscreener.scoring.point_in_time import build_moic_inputs
from autoscreener.scoring.valuation_context import (
    TickerValuationRow,
    compute_valuation_percentiles,
)
from autoscreener.screening.exclusion_gates import normalize_financial_currency_value
from autoscreener.validation.rules import sanitize_info

logger = logging.getLogger(__name__)


def config_hash(scoring_config: ScoringConfig) -> str:
    canonical = json.dumps(scoring_config.model_dump(), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_inputs_for_ticker(
    payload: dict, price_rows: list[PriceSnapshot], as_of: date, sector: str | None
) -> MoicInputs | None:
    """1銘柄分の `MoicInputs` を組み立てる。API・バックテストからも呼ばれる。

    価格・発行済株式数は `as_of` 以前の観測のみを使う。現在時点のスコアリング
    でも過去時点のバックテストでも同じ関数を通すことで、両者のロジックが
    ずれないことを構造的に保証する(27.8)。
    """
    share_observations: list[tuple[date, float | None]] = [
        (row.trade_date, float(row.shares_outstanding) if row.shares_outstanding is not None else None)
        for row in price_rows
    ]
    price_observations: list[tuple[date, float]] = [
        (row.trade_date, float(row.close)) for row in price_rows if row.close is not None
    ]
    return build_moic_inputs(
        payload=payload,
        share_observations=share_observations,
        price_observations=price_observations,
        as_of=as_of,
        sector=sector,
    )


def current_ev_to_gross_profit(inputs: MoicInputs) -> float | None:
    """現在のEV/GrossProfit。診断表示用(v4のモデル計算では回帰先として使わない)。"""
    enterprise_value = inputs.market_cap + inputs.net_debt
    if enterprise_value <= 0 or inputs.gross_profit_latest <= 0:
        return None
    return enterprise_value / inputs.gross_profit_latest


def cross_section_for(
    inputs_by_ticker: dict[int, MoicInputs], scoring_config: ScoringConfig
) -> CrossSection:
    """当日のユニバース全体から `CrossSection` を作る(28.5)。

    v3の `sector_median_multiples`(セクター別EV/粗利中央値)を置き換える。
    v4は終端マルチプルの平均回帰そのものを撤廃したため(28.2)、セクター中央値を
    必要とする箇所がモデルから消えた。代わりに必要なのは、ナウキャストの基準線と
    σ の縮小中心という、**ユニバース全体で1組**の値である。
    """
    return build_cross_section(list(inputs_by_ticker.values()), scoring_config)


def result_to_factors(result: MoicResult) -> dict[str, float]:
    """`MoicResult` のうち、DB(`scores.factors`)とAPIに載せる内訳と診断値。"""
    return {
        "expected_moic": result.expected_moic,
        "revenue_multiple": result.revenue_multiple,
        "margin_multiple": result.margin_multiple,
        "multiple_change": result.multiple_change,
        "leverage_effect": result.leverage_effect,
        "dilution_drag": result.dilution_drag,
        "size_prior": result.size_prior,
        "initial_growth_rate": result.initial_growth_rate,
        "base_growth_rate": result.base_growth_rate,
        "growth_nowcast_adjustment": result.growth_nowcast_adjustment,
        # 30章:循環性割引と終端バリュエーション上限の診断値。
        "statement_growth_rate": result.statement_growth_rate,
        "growth_cyclicality_adjustment": result.growth_cyclicality_adjustment,
        "terminal_multiple_capped": 1.0 if result.terminal_multiple_capped else 0.0,
        **(
            {"revenue_trend_consistency": result.revenue_trend_consistency}
            if result.revenue_trend_consistency is not None
            else {}
        ),
        **(
            {"gross_margin_consistency": result.gross_margin_consistency}
            if result.gross_margin_consistency is not None
            else {}
        ),
        "terminal_growth_rate": result.terminal_growth_rate,
        "growth_fade_rate": result.growth_fade_rate,
        "raw_log_moic_sigma": result.raw_log_moic_sigma,
        "terminal_gross_margin": result.terminal_gross_margin,
        "current_ev_to_gross_profit": result.current_ev_to_gross_profit,
        "target_ev_to_gross_profit": result.target_ev_to_gross_profit,
        "implied_terminal_ev": result.implied_terminal_ev,
        "health_index": result.health_index,
        # 2026-08-26追加(docs/model_audit_v4_2026-08-26.md)の診断フラグ。
        # `factors` は dict[str, float] なので bool は 0.0/1.0 で表す(S-6/A-1)。
        "growth_rate_clamped": 1.0 if result.growth_rate_clamped else 0.0,
        "dilution_data_missing": 1.0 if result.dilution_data_missing else 0.0,
        # E-1(2026-08-27、docs/defect_audit_2026-08-27.md):net_debtの構成要素欠損フラグ。
        "net_debt_data_missing": 1.0 if result.net_debt_data_missing else 0.0,
        **(
            {"lease_share_of_net_debt": result.lease_share_of_net_debt}
            if result.lease_share_of_net_debt is not None
            else {}
        ),
        # D-6:射影ネットデットの診断値(project_net_debt が無効でも計算・表示する)。
        **(
            {
                "projected_net_debt": result.projected_net_debt,
                "net_debt_change": result.net_debt_change,
            }
            if result.projected_net_debt is not None
            else {}
        ),
    }


def load_calibration_map(
    session: Session, scoring_config: ScoringConfig, current_config_hash: str
) -> CalibrationMap | None:
    """今の設定に対して妥当な較正写像を1つ取ってくる(28.8)。

    **`config_hash` の完全一致を要求する。** 較正写像は「この設定のモデルが
    出す確率は、実測ではこの頻度だった」という対応表であり、パラメータを1つ
    でも変えれば対応は崩れる。近い設定の写像で代用すると、較正済みという
    ラベルだけが残って中身が保証されない状態になる——それは較正しないより悪い。

    一致する実行が無ければ None を返し、スコアは未較正のまま保存される。
    UIは「設定変更後にバックテストが未実行」として、その状態を明示する。
    """
    if not scoring_config.calibration.enabled:
        return None
    run = (
        session.query(BacktestRun)
        .filter(
            BacktestRun.scoring_version == scoring_config.scoring_version,
            BacktestRun.config_hash == current_config_hash,
            BacktestRun.calibration_map.isnot(None),
        )
        .order_by(BacktestRun.run_at.desc())
        .first()
    )
    return CalibrationMap.from_dict(run.calibration_map) if run else None


def calibrated_on_pace_probability(
    result: MoicResult, calibration: CalibrationMap | None, scoring_config: ScoringConfig
) -> float | None:
    """実測で較正済みの「バックテストのホライズンでオンペースに乗る確率」(28.8)。

    `probability`(7年で10倍)とは別の量である。7年後の実測は原理的に今日
    存在しないので較正できないが、こちらは擬似バックテストが実際に観測して
    いる事象なので較正できる。**利用者が自分で答え合わせできる唯一の数字**。
    """
    if calibration is None or calibration.horizon_days <= 0:
        return None
    horizon_years = calibration.horizon_days / 365.25
    predicted = scale_probability_to_horizon(
        result.log_moic_mu,
        result.log_moic_sigma,
        scoring_config.target_moic,
        horizon_years,
        scoring_config.horizon_years,
    )
    return calibration.apply(predicted)


# `Score` のうち、スコアリングが毎回書き換えるカラム。
#
# **この集合が「書き漏らしてはいけない列」の唯一の定義である。** 更新は
# 既存行への setattr で行うため、値の辞書に無いカラムは前回実行の値がそのまま
# 残る。実際に `calibrated_on_pace_probability` の記載漏れで、ランキング対象から
# 外れた14銘柄に**以前ランキングされていたときの「1年オンペース率」が残り続けて
# いた**——順位が付かない銘柄の詳細画面に較正済み確率が出るという、27.20が防ごうと
# していた種類の誤情報そのものである。`_score_values` が全カラムを必ず埋める。
_MUTABLE_SCORE_COLUMNS = frozenset(
    {
        "config_hash",
        "inputs",
        "probability",
        "calibrated_on_pace_probability",
        "median_moic",
        "log_moic_mu",
        "log_moic_sigma",
        "survival_probability",
        "factors",
        # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):このスコアが読んだデータの日付。
        "price_as_of",
        "financials_as_of",
    }
)


def _score_values(
    config_hash_value: str,
    stored_inputs: dict,
    result: MoicResult,
    probability: float | None,
    calibrated: float | None,
    unranked_reason: str | None,
    price_as_of: date | None = None,
    financials_as_of: date | None = None,
    extra_factors: dict[str, float] | None = None,
) -> dict:
    """`Score` 行に書き込む値。**ランキング対象・対象外の両方がここを通る。**

    2箇所で別々にカラムを並べていたのが `calibrated_on_pace_probability` の
    書き漏れを生んだ。1つの関数に集約し、`_MUTABLE_SCORE_COLUMNS` との一致を
    アサートすることで、カラムを増やしたときの漏れを構造的に防ぐ。

    `extra_factors` は J-3 のバリュエーション分位のような**表示専用の追加値**を
    `factors` JSONB に混ぜるための口。モデル計算(`result`)には触れないので
    `probability` は動かない。
    """
    factors = result_to_factors(result)
    if extra_factors:
        factors = {**factors, **extra_factors}
    if unranked_reason is not None:
        factors = {**factors, "unranked_reason": unranked_reason}

    values = {
        "config_hash": config_hash_value,
        "inputs": stored_inputs,
        "probability": probability,
        "calibrated_on_pace_probability": calibrated,
        "median_moic": result.median_moic,
        "log_moic_mu": result.log_moic_mu,
        "log_moic_sigma": result.log_moic_sigma,
        "survival_probability": result.survival_probability,
        "factors": factors,
        "price_as_of": price_as_of,
        "financials_as_of": financials_as_of,
    }
    # カラムを追加したのにここを更新し忘れる、という事故を実行時に止める。
    assert set(values) == _MUTABLE_SCORE_COLUMNS, (
        f"_score_values が書くカラムと _MUTABLE_SCORE_COLUMNS が食い違っています: "
        f"{set(values) ^ _MUTABLE_SCORE_COLUMNS}"
    )
    return values


def _upsert_score(
    session: Session, ticker_id: int, score_date: date, scoring_version: str, values: dict
) -> None:
    """同一 (銘柄, 日付, バージョン) の行を作るか更新する(18.3のべき等性)。"""
    existing = (
        session.query(Score)
        .filter_by(ticker_id=ticker_id, score_date=score_date, scoring_version=scoring_version)
        .one_or_none()
    )
    if existing is None:
        session.add(
            Score(
                ticker_id=ticker_id,
                score_date=score_date,
                scoring_version=scoring_version,
                **values,
            )
        )
        return
    for field, value in values.items():
        setattr(existing, field, value)


def _load_scoring_universe(
    session: Session, score_date: date
) -> tuple[
    dict[int, MoicInputs],
    dict[int, str | None],
    dict[int, tuple[float | None, float | None]],
    dict[int, tuple[date | None, date | None]],
    int,
]:
    """当日ゲートを通過した銘柄の `MoicInputs` を組み立てる。

    戻り値は (入力, セクター, 規模, データ鮮度, 入力を組み立てられなかった銘柄数)。
    データ鮮度は銘柄ごとの (price_as_of, financials_as_of)。A-1(D-12)。

    「規模」は `info` 由来の (時価総額, TTM売上高) で、29章の**目標ごとの上限**を
    当てるために使う。`MoicInputs.market_cap`(株価×株式数)ではなく `info` を
    使うのは、除外ゲート(`apply_gates`)とAPIの絞り込みが同じ値を見ており、
    利用者が画面で見る時価総額とも一致するため——3つの場所で違う時価総額を
    使うと、「画面には$3.4Bと出ているのに$3.5Bの上限で消えた」が起きる。
    """
    included_ticker_ids = [
        row[0]
        for row in session.query(UniverseSnapshot.ticker_id).filter_by(snapshot_date=score_date, included=True).all()
    ]

    inputs_by_ticker: dict[int, MoicInputs] = {}
    sectors: dict[int, str | None] = {}
    scale: dict[int, tuple[float | None, float | None]] = {}
    as_of_by_ticker: dict[int, tuple[date | None, date | None]] = {}
    unmeasurable = 0

    for ticker_id in included_ticker_ids:
        # 14.3:`available_from` はまさにこのために用意された列(先読みバイアス
        # 対策)だが、2026-08-26まで**どこからも参照されていなかった**。
        # `run-scoring --date` で過去日を計算し直すとき、財務諸表は
        # `build_point_in_time_statements` が開示ラグで切ってくれるが、
        # スナップショットそのものの選択が「今日の最新」のままだと、
        # 当時まだ収集していなかった銘柄まで対象に入る。
        raw = (
            session.query(RawSnapshot)
            .filter(RawSnapshot.ticker_id == ticker_id, RawSnapshot.available_from <= score_date)
            .order_by(RawSnapshot.snapshot_date.desc())
            .first()
        )
        if raw is None:
            unmeasurable += 1
            continue
        price_rows = (
            session.query(PriceSnapshot)
            .filter(PriceSnapshot.ticker_id == ticker_id, PriceSnapshot.trade_date <= score_date)
            .order_by(PriceSnapshot.trade_date.asc())
            .all()
        )
        info = sanitize_info(raw.payload.get("info") or {})
        sector = info.get("sector")
        inputs = build_inputs_for_ticker(raw.payload, price_rows, score_date, sector)
        if inputs is None:
            unmeasurable += 1
            continue
        inputs_by_ticker[ticker_id] = inputs
        sectors[ticker_id] = sector
        scale[ticker_id] = (
            info.get("marketCap"),
            normalize_financial_currency_value(info.get("totalRevenue"), info),
        )
        as_of_by_ticker[ticker_id] = (
            price_rows[-1].trade_date if price_rows else None,
            raw.available_from,
        )

    return inputs_by_ticker, sectors, scale, as_of_by_ticker, unmeasurable


def primary_universe_inputs(
    inputs_by_ticker: dict[int, MoicInputs],
    scale: dict[int, tuple[float | None, float | None]],
    scoring_config: ScoringConfig,
    universe_config: UniverseConfig,
) -> dict[int, MoicInputs]:
    """既定の目標(7年で10倍)に対応する母集団だけを取り出す(29章)。

    **`CrossSection` はどの母集団で測ったかで意味が変わる。** ナウキャストの
    基準線(市場全体の動き)も σ の縮小中心も、母集団の中央値だからである。
    29章で materialize する母集団を「最も緩い目標」まで広げたため、断面統計を
    materialize 済みの全体から取ると、**既定の目標のランキングが、本来その目標の
    候補ではない銘柄の影響で動く**ことになる。

    バックテスト(`_evaluate_one_date`)は「検証している目標の母集団」から断面を
    作っており、そこで測ったKPIが v4 の較正の根拠になっている。ライブもそれに
    合わせる:保存する断面は**既定の目標の母集団**のもので、目標を変えた
    リクエストはAPIがその目標の母集団で断面を作り直す(`routes._cross_section_for_target`)。
    """
    ceilings = universe_config.ceilings_for_target(scoring_config.target_moic)
    primary: dict[int, MoicInputs] = {}
    for ticker_id, inputs in inputs_by_ticker.items():
        market_cap, revenue = scale.get(ticker_id, (None, None))
        if market_cap is not None and market_cap >= ceilings.market_cap_usd:
            continue
        if revenue is not None and revenue >= ceilings.revenue_usd:
            continue
        primary[ticker_id] = inputs
    return primary


def _check_price_freshness(
    session: Session, score_date: date, scoring_config: ScoringConfig
) -> str | None:
    """A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):スコアリングを走らせてよい
    データ鮮度か。問題があれば `skipped_reason` 文字列を、無ければ None を返す。

    2つの前提を確認する:
      1. 最新の price_snapshots が `max_price_staleness_days` 営業日以内であること
      2. 当日ゲート通過銘柄のうち、その最新取引日の価格行を持つ割合が
         `min_same_day_price_coverage` 以上であること(=収集が途中で落ちていない)
    """
    freshness = scoring_config.freshness
    max_price_date = session.query(func.max(PriceSnapshot.trade_date)).scalar()
    if max_price_date is None:
        return "no_price_data"

    staleness = business_days_between(max_price_date, score_date)
    if staleness > freshness.max_price_staleness_days:
        return (
            f"stale_price_data (latest {max_price_date} is {staleness} business days "
            f"before {score_date}, limit {freshness.max_price_staleness_days})"
        )

    included_ids = [
        row[0]
        for row in session.query(UniverseSnapshot.ticker_id)
        .filter_by(snapshot_date=score_date, included=True)
        .all()
    ]
    if included_ids:
        with_latest = (
            session.query(func.count(func.distinct(PriceSnapshot.ticker_id)))
            .filter(
                PriceSnapshot.ticker_id.in_(included_ids),
                PriceSnapshot.trade_date == max_price_date,
            )
            .scalar()
            or 0
        )
        coverage = with_latest / len(included_ids)
        if coverage < freshness.min_same_day_price_coverage:
            return (
                f"insufficient_price_coverage ({coverage:.1%} of {len(included_ids)} gated "
                f"tickers have a {max_price_date} price row, need "
                f"{freshness.min_same_day_price_coverage:.0%})"
            )
    return None


def run_scoring(
    score_date: date | None = None,
    scoring_config: ScoringConfig | None = None,
    universe_config: UniverseConfig | None = None,
) -> dict[str, int]:
    score_date = score_date or utc_today()
    scoring_config = scoring_config or load_scoring_config()
    universe_config = universe_config or load_universe_config()
    current_config_hash = config_hash(scoring_config)

    with session_scope() as session:
        # スコアリング対象は「`score_date` 当日のゲート判定を通過した銘柄」に限定する。
        # その日の apply_gates がまだ走っていない場合に前日のゲート結果で当日付の
        # Score行を書くと、API(`GET /candidates`)が同日の universe_snapshots と
        # 突合するため1件も表示されない(原因の分かりにくい空ランキング)。
        has_universe_for_date = (
            session.query(UniverseSnapshot.id).filter_by(snapshot_date=score_date).limit(1).scalar() is not None
        )
        if not has_universe_for_date:
            logger.error(
                "no universe_snapshots rows for %s — run apply-gates for the same date before scoring", score_date
            )
            return {"scored": 0, "unmeasurable": 0}

        # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):データ鮮度の前提条件。
        # 収集が一斉隔離・レート制限・ネットワーク断で途中停止すると、ここに
        # 到達しても最新の price_snapshots が数日前で止まっている。その状態で
        # スコアを書くと、古い株価のランキングが今日付で出て、その事実がどこにも
        # 表示されない。**中止するほうが安全**である(D-12)。
        stale = _check_price_freshness(session, score_date, scoring_config)
        if stale is not None:
            logger.error("run_scoring aborted for %s: %s", score_date, stale)
            return {"scored": 0, "negative_outlook": 0, "unmeasurable": 0, "skipped_reason": stale}

        inputs_by_ticker, sectors, scale, as_of_by_ticker, unmeasurable = _load_scoring_universe(
            session, score_date
        )
        # 29章:スコア自体は materialize 済みの母集団すべてに対して計算する
        # (目標を緩めたときにAPIが再計算できるよう入力を保存しておくため)が、
        # 断面統計は既定の目標の母集団から作る。理由は `primary_universe_inputs`。
        cross_section = cross_section_for(
            primary_universe_inputs(inputs_by_ticker, scale, scoring_config, universe_config),
            scoring_config,
        )
        stored_cross_section = cross_section.to_dict()

        # J-3(docs/investment_decision_gap_2026-08-29.md):同じ日の断面での
        # バリュエーション分位。**表示専用**であり `factors` JSONB に混ぜるだけ
        # ——`compute_moic` には渡さないので順位は動かない。
        valuation_percentiles = compute_valuation_percentiles(
            [
                TickerValuationRow(
                    ticker_id=ticker_id,
                    sector=sectors.get(ticker_id),
                    ev_to_gross_profit=current_ev_to_gross_profit(inputs),
                    revenue_growth=(
                        inputs.revenue_cagr if inputs.revenue_cagr is not None else inputs.revenue_yoy
                    ),
                    gross_margin=inputs.gross_margin_latest,
                )
                for ticker_id, inputs in inputs_by_ticker.items()
            ]
        )

        calibration = load_calibration_map(session, scoring_config, current_config_hash)
        if calibration is None and scoring_config.calibration.enabled:
            logger.warning(
                "no calibration map for %s/%s — scores will be stored uncalibrated; "
                "run `run-backtest` after changing config/scoring.yaml",
                scoring_config.scoring_version,
                current_config_hash,
            )

        def _valuation_extra(ticker_id: int) -> dict[str, float]:
            return {
                key: value
                for key, value in valuation_percentiles.get(ticker_id, {}).items()
                if value is not None
            }

        scored_count = 0
        unranked_count = 0
        scored_ticker_ids: set[int] = set()

        for ticker_id, inputs in inputs_by_ticker.items():
            result = compute_moic(inputs, cross_section, scoring_config)
            if result is None:
                # 27.20:ランキング外の理由を2つに分ける。閾値判定を外して
                # 計算し直し、それでも None なら「測れない」、値が返れば
                # 「測れたが見通しがマイナス」。後者には確率NULLの Score 行を
                # 書き、期待倍率とともに理由を残す——実データではこちらが
                # ランキング外の71%を占めており、「データ不足」と表示するのは
                # 誤情報になる。
                unranked = compute_moic(
                    inputs, cross_section, scoring_config, enforce_min_expected_moic=False
                )
                if unranked is None:
                    unmeasurable += 1
                    continue
                # 27.20:見通しがマイナスの銘柄は確率NULLで記録する。順位は
                # 付けないが、期待倍率と5因子の内訳は残す——「測れなかった」
                # ではなく「測った結果がこうだった」という情報のほうが有用。
                # 確率が無い以上、それを較正した値も存在しえないので None。
                _upsert_score(
                    session,
                    ticker_id,
                    score_date,
                    scoring_config.scoring_version,
                    _score_values(
                        current_config_hash,
                        {**inputs.to_dict(), "cross_section": stored_cross_section},
                        unranked,
                        probability=None,
                        calibrated=None,
                        unranked_reason="negative_outlook",
                        price_as_of=as_of_by_ticker.get(ticker_id, (None, None))[0],
                        financials_as_of=as_of_by_ticker.get(ticker_id, (None, None))[1],
                        extra_factors=_valuation_extra(ticker_id),
                    ),
                )
                unranked_count += 1
                scored_ticker_ids.add(ticker_id)
                continue

            _upsert_score(
                session,
                ticker_id,
                score_date,
                scoring_config.scoring_version,
                _score_values(
                    current_config_hash,
                    {**inputs.to_dict(), "cross_section": stored_cross_section},
                    result,
                    probability=result.probability,
                    calibrated=calibrated_on_pace_probability(result, calibration, scoring_config),
                    unranked_reason=None,
                    price_as_of=as_of_by_ticker.get(ticker_id, (None, None))[0],
                    financials_as_of=as_of_by_ticker.get(ticker_id, (None, None))[1],
                    extra_factors=_valuation_extra(ticker_id),
                ),
            )

            scored_count += 1
            scored_ticker_ids.add(ticker_id)

        # L-12: preserve the rank generated by this run. It is score output
        # metadata only and never participates in the probability calculation.
        ranked_rows = (
            session.query(Score)
            .filter(
                Score.score_date == score_date,
                Score.scoring_version == scoring_config.scoring_version,
                Score.probability.isnot(None),
            )
            .order_by(Score.probability.desc())
            .all()
        )
        for rank, row in enumerate(ranked_rows, start=1):
            row.factors = {**(row.factors or {}), "rank_default_target": rank}

        # 同じ (score_date, scoring_version) で以前の実行が書いた行のうち、今回の
        # 対象から外れたものを削除する(18.3:再実行すれば同じ状態に収束すること
        # もべき等性のうち)。残すと「その日のスコア」の集合が実行履歴に依存する。
        stale_query = session.query(Score).filter(
            Score.score_date == score_date,
            Score.scoring_version == scoring_config.scoring_version,
        )
        if scored_ticker_ids:
            stale_query = stale_query.filter(Score.ticker_id.notin_(scored_ticker_ids))
        stale_count = stale_query.delete(synchronize_session=False)
        if stale_count:
            logger.info("removed %d stale score rows for %s/%s", stale_count, score_date, scoring_config.scoring_version)

    return {"scored": scored_count, "negative_outlook": unranked_count, "unmeasurable": unmeasurable}
