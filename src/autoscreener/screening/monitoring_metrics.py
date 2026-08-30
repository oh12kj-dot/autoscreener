"""保有銘柄の四半期モニタリング指標(30.7.3)。

元文書 第12節の表の上4行。**すべて既存データから計算できる**(新規取得ゼロ)。
**既存関数を必ず再利用する**——`exclusion_gates.parse_period_series` /
`compute_cash_runway_quarters` / `normalize_financial_currency_value`
(13.5の通貨混在対策)がすでにある。同じ計算を2箇所に書くと必ず片方だけ直される。

**閾値は売却条件ではない。** 点灯したら「価格に関係なく判断をやり直す」ための
合図であり、機械的な売りシグナルとして使ってはならない(元文書 第11節 売却規律)。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from autoscreener.screening.customer_concentration import concentration_drop
from autoscreener.screening.exclusion_gates import (
    compute_cash_runway_quarters,
    parse_period_series,
)

REVENUE_GROWTH_DECELERATION = "revenue_growth_deceleration"
GROSS_MARGIN_DECLINE = "gross_margin_decline"
SHARE_COUNT_GROWTH = "share_count_growth"
CASH_RUNWAY_LOW = "cash_runway_low"
# K-2/K-3(自動化計画 2026-08-30):`research/TEMPLATE.md` のプレモーテムが
# 要求していたのに実装が無かった先行指標。`customer_concentration` テーブル
# (K-3で新設)の履歴を根拠に判定する。
CUSTOMER_CONCENTRATION_DISCLOSED_DROP = "customer_concentration_disclosed_drop"

METRIC_LABELS: dict[str, str] = {
    REVENUE_GROWTH_DECELERATION: "売上成長率の減速",
    GROSS_MARGIN_DECLINE: "粗利率の低下",
    SHARE_COUNT_GROWTH: "株式数の急増",
    CASH_RUNWAY_LOW: "キャッシュランウェイ不足",
    CUSTOMER_CONCENTRATION_DISCLOSED_DROP: "顧客集中開示の消失/低下",
}


@dataclass(frozen=True)
class MonitoringThresholds:
    revenue_growth_deceleration_quarters: int = 2
    gross_margin_decline_quarters: int = 2
    share_count_annual_growth_ceiling: float = 0.15
    cash_runway_floor_months: float = 12.0
    # K-3:前年から絶対値でこのポイント数以上、最大顧客の集中度が落ちたら点灯。
    # `concentration_drop()` のもう1つの発火条件(開示自体の消失)には
    # しきい値が無い(消えたか消えていないかの二値なので不要)。
    concentration_drop_pct_points: float = 0.05


@dataclass(frozen=True)
class MonitoringMetric:
    code: str
    label: str
    current_value: float | None
    previous_value: float | None
    triggered: bool
    detail: str


def _yoy_growth_series(revenue_series: dict) -> list[tuple[datetime.date, float]]:
    """四半期売上高から前年同期比(YoY)成長率の系列を作る。

    厳密な四半期アラインメント(同じ暦四半期)ではなく、**直近4期前との比較**
    (`points[i] vs points[i-4]`)で近似する——yfinanceの四半期データは会計期末が
    会社ごとに異なり、暦四半期に正規化されていないため。決算1回=1期という
    前提のもと「4期前」が概ね1年前に対応する。
    """
    points = parse_period_series(revenue_series)
    growth: list[tuple] = []
    for i in range(4, len(points)):
        _prev_date, prev_value = points[i - 4]
        cur_date, cur_value = points[i]
        if prev_value == 0:
            continue
        growth.append((cur_date, (cur_value - prev_value) / abs(prev_value)))
    return growth


def _consecutive_decline(values: list[float], n: int) -> bool:
    """末尾 `n` 件が単調減少しているか(=直近 n-1 回連続で前回を下回るか)。"""
    if len(values) < n:
        return False
    tail = values[-n:]
    return all(tail[i] < tail[i - 1] for i in range(1, len(tail)))


def _revenue_growth_metric(quarterly_income_stmt: dict, thresholds: MonitoringThresholds) -> MonitoringMetric:
    revenue_series = quarterly_income_stmt.get("Total Revenue") or {}
    growth = _yoy_growth_series(revenue_series)
    growth_values = [g for _, g in growth]
    triggered = _consecutive_decline(growth_values, thresholds.revenue_growth_deceleration_quarters)
    current = growth_values[-1] if growth_values else None
    previous = growth_values[-2] if len(growth_values) >= 2 else None
    return MonitoringMetric(
        code=REVENUE_GROWTH_DECELERATION,
        label=METRIC_LABELS[REVENUE_GROWTH_DECELERATION],
        current_value=current,
        previous_value=previous,
        triggered=triggered,
        detail=(
            f"直近{thresholds.revenue_growth_deceleration_quarters}四半期連続で"
            "売上成長率(YoY)が減速しています。"
            if triggered
            else "売上成長率は減速していません。"
        ),
    )


def _gross_margin_metric(quarterly_income_stmt: dict, thresholds: MonitoringThresholds) -> MonitoringMetric:
    revenue_points = dict(parse_period_series(quarterly_income_stmt.get("Total Revenue")))
    profit_points = dict(parse_period_series(quarterly_income_stmt.get("Gross Profit")))
    common_dates = sorted(set(revenue_points) & set(profit_points))
    margins = [
        profit_points[d] / revenue_points[d] for d in common_dates if revenue_points[d] not in (0, None)
    ]
    triggered = _consecutive_decline(margins, thresholds.gross_margin_decline_quarters)
    current = margins[-1] if margins else None
    previous = margins[-2] if len(margins) >= 2 else None
    return MonitoringMetric(
        code=GROSS_MARGIN_DECLINE,
        label=METRIC_LABELS[GROSS_MARGIN_DECLINE],
        current_value=current,
        previous_value=previous,
        triggered=triggered,
        detail=(
            f"直近{thresholds.gross_margin_decline_quarters}四半期連続で粗利率が低下しています。"
            if triggered
            else "粗利率は低下していません。"
        ),
    )


def _share_count_metric(
    share_counts: list[tuple[datetime.date, int]], thresholds: MonitoringThresholds
) -> MonitoringMetric:
    """`share_counts` は (日付, 株式数) の昇順リスト(`price_snapshots.shares_outstanding`)。"""
    ordered = sorted(share_counts, key=lambda p: p[0])
    if len(ordered) < 2:
        return MonitoringMetric(
            code=SHARE_COUNT_GROWTH,
            label=METRIC_LABELS[SHARE_COUNT_GROWTH],
            current_value=None,
            previous_value=None,
            triggered=False,
            detail="株式数の履歴が不足しているため判定できません。",
        )
    first_date, first_count = ordered[0]
    last_date, last_count = ordered[-1]
    days = (last_date - first_date).days
    if days <= 0 or first_count in (0, None) or last_count is None:
        annual_growth = None
    else:
        total_growth = (last_count - first_count) / first_count
        annual_growth = total_growth * (365.0 / days)
    triggered = annual_growth is not None and annual_growth > thresholds.share_count_annual_growth_ceiling
    return MonitoringMetric(
        code=SHARE_COUNT_GROWTH,
        label=METRIC_LABELS[SHARE_COUNT_GROWTH],
        current_value=annual_growth,
        previous_value=None,
        triggered=triggered,
        detail=(
            f"発行済株式数が年率換算で{thresholds.share_count_annual_growth_ceiling:.0%}を"
            "超えて増加しています。"
            if triggered
            else "発行済株式数の増加は正常範囲内です。"
        ),
    )


def _cash_runway_metric(
    total_cash: float | None, quarterly_cash_flow: dict, thresholds: MonitoringThresholds
) -> MonitoringMetric:
    runway_quarters = compute_cash_runway_quarters(total_cash, quarterly_cash_flow)
    floor_quarters = thresholds.cash_runway_floor_months / 3.0
    triggered = runway_quarters is not None and runway_quarters < floor_quarters
    runway_months = runway_quarters * 3.0 if runway_quarters is not None and runway_quarters != float("inf") else runway_quarters
    return MonitoringMetric(
        code=CASH_RUNWAY_LOW,
        label=METRIC_LABELS[CASH_RUNWAY_LOW],
        current_value=runway_months,
        previous_value=None,
        triggered=triggered,
        detail=(
            f"キャッシュランウェイが{thresholds.cash_runway_floor_months:.0f}か月を下回っています。"
            if triggered
            else "キャッシュランウェイは十分です。"
        ),
    )


def evaluate_customer_concentration_metric(
    concentration_history: list[tuple[datetime.date, float | None]], thresholds: MonitoringThresholds
) -> MonitoringMetric:
    """`concentration_history` は `(決算期末, その期の最大顧客集中度)` のリスト
    (`customer_concentration` テーブルを期ごとに集計したもの。`screening.
    customer_concentration.concentration_drop` と同じ形——判定ロジック自体は
    そちらに委譲し、ここは他の `_xxx_metric` 関数と同じ入出力の形に揃える薄い
    ラッパーに留める)。

    値が `None` の期は「その期の10-Kは処理したが10%超の顧客開示が無かった」
    ことを表す(呼び出し側は10-Kを見つけられなかった期をリストに含めない
    ——「開示が消えた」と「データが無い」を混同しないため)。

    既存の4指標(`_gross_margin_metric` 等)と違い、この指標が要求するデータ
    (`customer_concentration` の履歴)は `evaluate_monitoring()` の既存引数
    (yfinanceのスナップショット由来)には含まれていない。**そのため
    `evaluate_monitoring()` のシグネチャ・戻り値件数はあえて変更しない**
    ——`batch/run_monitoring.py` の既存呼び出しと `tests/unit/
    test_monitoring_metrics.py` の `len(metrics) == 4` を壊さないため。
    呼び出し側(パイプライン配線)がこの関数を直接呼び、`_record_alert` に
    渡す形で接続する想定。
    """
    result = concentration_drop(
        concentration_history, drop_threshold_points=thresholds.concentration_drop_pct_points
    )
    if result.reason == "disclosure_disappeared":
        detail = "前期は10%超の顧客開示があったが、直近期でその開示が消えました。"
    elif result.reason == "pct_dropped":
        detail = (
            f"最大顧客の売上構成比が前期比で{thresholds.concentration_drop_pct_points:.0%}"
            "ポイント以上低下しました。"
        )
    else:
        detail = "顧客集中度の開示に大きな変化はありません。"
    return MonitoringMetric(
        code=CUSTOMER_CONCENTRATION_DISCLOSED_DROP,
        label=METRIC_LABELS[CUSTOMER_CONCENTRATION_DISCLOSED_DROP],
        current_value=result.current_max_pct,
        previous_value=result.previous_max_pct,
        triggered=result.triggered,
        detail=detail,
    )


def evaluate_monitoring(
    quarterly_income_stmt: dict,
    quarterly_cash_flow: dict,
    total_cash: float | None,
    share_counts: list[tuple],
    thresholds: MonitoringThresholds,
) -> list[MonitoringMetric]:
    """4指標すべてを評価する。"""
    return [
        _revenue_growth_metric(quarterly_income_stmt, thresholds),
        _gross_margin_metric(quarterly_income_stmt, thresholds),
        _share_count_metric(share_counts, thresholds),
        _cash_runway_metric(total_cash, quarterly_cash_flow, thresholds),
    ]
