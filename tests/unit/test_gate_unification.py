"""B-5(defect_and_edge_audit_2026-08-28.md D-10)のテスト:ライブと同一の
`evaluate_gates` を、ポイントインタイム値で組み立てた `GateInput` に適用する。

`point_in_time.build_gate_input` は純粋関数(DBに触らない)。
"""

from __future__ import annotations

import datetime

from autoscreener.config import load_universe_config
from autoscreener.scoring.moic import MoicInputs
from autoscreener.scoring.point_in_time import build_gate_input
from autoscreener.screening.exclusion_gates import evaluate_gates

AS_OF = datetime.date(2025, 6, 30)


def _inputs(**overrides) -> MoicInputs:
    base = dict(
        market_cap=5.0e8,
        net_debt=-1.0e7,
        revenue_latest=8.0e7,
        gross_profit_latest=4.0e7,
        revenue_cagr=0.3,
        revenue_yoy=0.35,
        revenue_growth_volatility=0.2,
        gross_margin_latest=0.5,
        gross_margin_prior=0.48,
        dilution_cagr=0.05,
        piotroski_ratio=0.6,
        cash_runway_quarters=10.0,
        equity_to_assets=0.5,
        fcf_margin=-0.1,
        sector="Technology",
    )
    base.update(overrides)
    return MoicInputs(**base)


def _payload(quarterly_fcf: dict | None = None, annual_periods: int = 3) -> dict:
    ends = [f"{2022 + i}-12-31" for i in range(annual_periods)]
    payload = {
        "income_stmt": {"Total Revenue": {e: 5.0e7 * (1.3**i) for i, e in enumerate(ends)}},
        "balance_sheet": {
            "Stockholders Equity": {ends[-1]: 1.0e8},
            "Cash And Cash Equivalents": {ends[-1]: 2.0e7},
        },
        "cash_flow": {"Free Cash Flow": {ends[-1]: -8.0e6}},
    }
    if quarterly_fcf is not None:
        payload["quarterly_cash_flow"] = {"Free Cash Flow": quarterly_fcf}
        payload["quarterly_income_stmt"] = {
            "Total Revenue": {f"2024-{m:02d}-30": 1.0e7 for m in (3, 6, 9, 12)}
        }
    return payload


def test_cash_runway_reconstructed_from_quarterly_and_can_fail_the_live_gate():
    """四半期FCFを開示ラグで切って再構成し、ランウェイ不足なら evaluate_gates が落とす。

    これが D-10 の核心:以前の backtest 専用ゲートは cash_runway を無視しており、
    脆弱・高ボラ銘柄をライブでだけ削っていた。
    """
    universe = load_universe_config()
    # 現金が少なく四半期バーンが大きい -> ランウェイ < 6四半期。
    quarterly_fcf = {f"2024-{m:02d}-30": -5.0e6 for m in (3, 6, 9, 12)}
    payload = _payload(quarterly_fcf=quarterly_fcf)
    payload["balance_sheet"]["Cash And Cash Equivalents"] = {"2024-12-31": 6.0e6}

    gi = build_gate_input(payload, AS_OF, _inputs(), price=12.0, median_dollar_volume=5.0e6, min_annual_periods=2)
    assert gi.cash_runway_quarters is not None and gi.cash_runway_quarters < 6.0
    result = evaluate_gates(gi, universe)
    assert "cash_runway_floor" in result.reasons


def test_healthy_company_passes_the_unified_gate():
    universe = load_universe_config()
    gi = build_gate_input(_payload(), AS_OF, _inputs(), price=12.0, median_dollar_volume=5.0e6, min_annual_periods=2)
    assert evaluate_gates(gi, universe).passed


def test_annual_periods_backfill_listing_history_when_quarterly_unavailable():
    """四半期が全く無くても、年次期数が十分なら insufficient_listing_history にしない。"""
    universe = load_universe_config()
    gi = build_gate_input(
        _payload(annual_periods=3), AS_OF, _inputs(), price=12.0, median_dollar_volume=5.0e6, min_annual_periods=2
    )
    assert gi.available_quarters >= universe.min_listed_quarters
    assert "insufficient_listing_history" not in evaluate_gates(gi, universe).reasons


def test_reporting_lag_hides_recent_quarters():
    """期末 + 90日 を超えていない四半期は見えない(先読み防止)。"""
    # as_of の 30日前が期末の四半期は、開示ラグ90日により不可視。
    recent_end = (AS_OF - datetime.timedelta(days=30)).isoformat()
    quarterly_fcf = {recent_end: -5.0e6}
    gi = build_gate_input(
        _payload(quarterly_fcf=quarterly_fcf), AS_OF, _inputs(), price=12.0, median_dollar_volume=5.0e6, min_annual_periods=2
    )
    # 四半期が可視化されないので、年次FCFベースのランウェイにフォールバックする。
    assert gi.cash_runway_quarters is not None
