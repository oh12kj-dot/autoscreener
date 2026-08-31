"""ポイントインタイム再構成のテスト(27.8)。

**先読みバイアスが1箇所でも入るとバックテストの結論が丸ごと無意味になる。**
しかも先読みは例外を投げず、ただ結果を良く見せるだけなので、テストで固定して
おかないと検出できない。このファイルの主眼はそこにある。
"""

import datetime
import math

import pytest

from autoscreener.scoring.point_in_time import (
    REPORTING_LAG_DAYS,
    annualized_log_momentum,
    build_moic_inputs,
    build_point_in_time_statements,
    financial_to_trading_rate,
    revenue_cagr,
    revenue_growth_volatility,
    revenue_yoy,
    shares_outstanding_at,
)

PAYLOAD = {
    "info": {"sector": "Technology"},
    "income_stmt": {
        "Total Revenue": {
            "2021-12-31": 100.0,
            "2022-12-31": 130.0,
            "2023-12-31": 170.0,
            "2024-12-31": 220.0,
            "2025-12-31": 300.0,
        },
        "Gross Profit": {
            "2021-12-31": 50.0,
            "2022-12-31": 66.0,
            "2023-12-31": 88.0,
            "2024-12-31": 116.0,
            "2025-12-31": 160.0,
        },
        "Net Income": {"2024-12-31": 10.0, "2025-12-31": 15.0},
    },
    "balance_sheet": {
        "Total Assets": {"2024-12-31": 400.0, "2025-12-31": 500.0},
        "Total Debt": {"2024-12-31": 50.0, "2025-12-31": 60.0},
        "Cash And Cash Equivalents": {"2024-12-31": 30.0, "2025-12-31": 40.0},
        "Stockholders Equity": {"2024-12-31": 200.0, "2025-12-31": 260.0},
        "Ordinary Shares Number": {
            "2021-12-31": 10.0,
            "2022-12-31": 11.0,
            "2023-12-31": 12.0,
            "2024-12-31": 13.0,
            "2025-12-31": 14.0,
        },
    },
    "cash_flow": {
        "Free Cash Flow": {"2024-12-31": 5.0, "2025-12-31": 8.0},
        "Operating Cash Flow": {"2024-12-31": 12.0, "2025-12-31": 20.0},
    },
}


def _points(pairs):
    return [(datetime.date.fromisoformat(d), v) for d, v in pairs]


# --- 開示ラグ -----------------------------------------------------------------


def test_period_is_invisible_before_the_reporting_lag_elapses():
    """期末の翌日にその期の決算が使えてしまえば、それは先読みである。"""
    just_after_year_end = datetime.date(2026, 1, 5)
    statements = build_point_in_time_statements(PAYLOAD, just_after_year_end)
    assert datetime.date(2025, 12, 31) not in statements.visible_period_ends
    assert datetime.date(2024, 12, 31) in statements.visible_period_ends


def test_period_becomes_visible_after_the_reporting_lag():
    after_lag = datetime.date(2025, 12, 31) + datetime.timedelta(days=REPORTING_LAG_DAYS)
    statements = build_point_in_time_statements(PAYLOAD, after_lag)
    assert datetime.date(2025, 12, 31) in statements.visible_period_ends


def test_all_statements_are_filtered_consistently():
    """損益計算書だけ古い期に絞られて貸借対照表は最新、といった不整合を防ぐ。"""
    as_of = datetime.date(2025, 6, 1)  # FY2024 まで見える
    statements = build_point_in_time_statements(PAYLOAD, as_of)
    assert max(statements.income_stmt["Total Revenue"]) == "2024-12-31"
    assert max(statements.balance_sheet["Total Assets"]) == "2024-12-31"
    assert max(statements.cash_flow["Free Cash Flow"]) == "2024-12-31"


def test_visible_period_count_grows_over_time():
    counts = [
        len(build_point_in_time_statements(PAYLOAD, datetime.date(year, 6, 1)).visible_period_ends)
        for year in (2022, 2023, 2024, 2025, 2026)
    ]
    assert counts == sorted(counts)
    assert counts[0] < counts[-1]


# --- 成長率 -------------------------------------------------------------------


def test_revenue_cagr_annualises_over_the_actual_elapsed_period():
    points = _points([("2022-12-31", 100.0), ("2025-12-31", 200.0)])
    cagr = revenue_cagr(points, 3)
    assert cagr == pytest.approx(2 ** (1 / 3) - 1, rel=1e-3)


def test_revenue_cagr_needs_a_long_enough_window():
    points = _points([("2025-06-30", 100.0), ("2025-12-31", 120.0)])
    assert revenue_cagr(points, 3) is None


def test_revenue_yoy_uses_the_two_most_recent_periods():
    points = _points([("2023-12-31", 100.0), ("2024-12-31", 150.0), ("2025-12-31", 300.0)])
    assert revenue_yoy(points) == pytest.approx(1.0)


def test_revenue_yoy_is_undefined_when_the_base_period_is_not_positive():
    points = _points([("2024-12-31", 0.0), ("2025-12-31", 300.0)])
    assert revenue_yoy(points) is None


def test_growth_volatility_needs_three_growth_rates():
    assert revenue_growth_volatility(_points([("2024-12-31", 1.0), ("2025-12-31", 2.0)])) is None
    steady = _points(
        [("2022-12-31", 100.0), ("2023-12-31", 110.0), ("2024-12-31", 121.0), ("2025-12-31", 133.1)]
    )
    assert revenue_growth_volatility(steady) == pytest.approx(0.0, abs=1e-9)


# --- 株式数 -------------------------------------------------------------------


def test_shares_outstanding_ignores_observations_after_as_of():
    """評価日より後の株式数を使うのは、増資の結果を先に知ることに等しい。"""
    observations = _points([("2024-01-01", 10.0), ("2025-01-01", 20.0), ("2026-01-01", 40.0)])
    assert shares_outstanding_at(observations, datetime.date(2025, 6, 1)) == 20.0


def test_shares_outstanding_is_none_before_any_observation():
    observations = _points([("2025-01-01", 20.0)])
    assert shares_outstanding_at(observations, datetime.date(2024, 1, 1)) is None


# --- 統合 ---------------------------------------------------------------------


def test_build_moic_inputs_reflects_only_visible_data():
    """同じペイロードでも、評価日が違えば別の入力になること。"""
    early = build_moic_inputs(PAYLOAD, [], price_observations=[(datetime.date(2025, 6, 1), 10.0)], as_of=datetime.date(2025, 6, 1), sector="Technology")
    late = build_moic_inputs(PAYLOAD, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)], as_of=datetime.date(2026, 6, 1), sector="Technology")
    assert early is not None and late is not None
    assert early.revenue_latest == 220.0  # FY2024
    assert late.revenue_latest == 300.0  # FY2025
    # 株式数も可視期に応じて変わる(=時価総額も変わる)
    assert early.market_cap == 10.0 * 13.0
    assert late.market_cap == 10.0 * 14.0


def test_build_moic_inputs_computes_net_debt_from_visible_balance_sheet():
    inputs = build_moic_inputs(
        PAYLOAD, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)], as_of=datetime.date(2026, 6, 1), sector="Technology"
    )
    assert inputs is not None
    assert inputs.net_debt == pytest.approx(60.0 - 40.0)


def test_build_moic_inputs_extracts_lease_liability_when_present():
    """S-5(docs/model_audit_v4_2026-08-26.md): `Capital Lease Obligations` を
    net_debt とは独立に持ち回り、リースの多い企業(DBI型)をUIで警告できる
    ようにする。net_debt の計算自体は変えない。"""
    payload_with_lease = {
        **PAYLOAD,
        "balance_sheet": {
            **PAYLOAD["balance_sheet"],
            "Capital Lease Obligations": {"2024-12-31": 20.0, "2025-12-31": 45.0},
        },
    }
    inputs = build_moic_inputs(
        payload_with_lease, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)],
        as_of=datetime.date(2026, 6, 1), sector="Technology",
    )
    assert inputs is not None
    assert inputs.lease_liability == pytest.approx(45.0)
    # net_debt はリースの有無で変わらない(診断値と計算式を分離してあるため)
    assert inputs.net_debt == pytest.approx(60.0 - 40.0)


def test_missing_total_debt_sets_net_debt_data_missing_flag():
    """E-1(docs/defect_audit_2026-08-27.md): `Total Debt` の行が無い銘柄で、
    net_debt が黙って「無借金(=有利)」として計算されていることを可視化する
    診断フラグが立つこと(A-1と同型の欠陥への回帰テスト)。net_debt の計算式
    自体はまだ変えない(実データ調査と run-backtest 確認が済むまで)。"""
    payload_without_debt = {
        **PAYLOAD,
        "balance_sheet": {k: v for k, v in PAYLOAD["balance_sheet"].items() if k != "Total Debt"},
    }
    inputs = build_moic_inputs(
        payload_without_debt, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)],
        as_of=datetime.date(2026, 6, 1), sector="Technology",
    )
    assert inputs is not None
    assert inputs.net_debt_data_missing is True
    # 現行挙動:欠損は 0.0 で補完される(net_debt = 0 - cash)。
    assert inputs.net_debt == pytest.approx((0.0 - 40.0))


def test_missing_cash_rows_set_net_debt_data_missing_flag():
    """E-1: 現金系の行がすべて無い場合も診断フラグが立つこと。"""
    cash_rows = {
        "Cash Cash Equivalents And Short Term Investments",
        "Cash And Cash Equivalents",
        "Cash Equivalents",
        "Cash Financial",
    }
    payload_without_cash = {
        **PAYLOAD,
        "balance_sheet": {k: v for k, v in PAYLOAD["balance_sheet"].items() if k not in cash_rows},
    }
    inputs = build_moic_inputs(
        payload_without_cash, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)],
        as_of=datetime.date(2026, 6, 1), sector="Technology",
    )
    assert inputs is not None
    assert inputs.net_debt_data_missing is True


def test_net_debt_data_missing_flag_is_false_when_both_components_present():
    """E-1: 両方の構成要素が揃っていればフラグは立たない。"""
    inputs = build_moic_inputs(
        PAYLOAD, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)],
        as_of=datetime.date(2026, 6, 1), sector="Technology",
    )
    assert inputs is not None
    assert inputs.net_debt_data_missing is False


def test_build_moic_inputs_lease_liability_is_none_when_absent():
    inputs = build_moic_inputs(
        PAYLOAD, [], price_observations=[(datetime.date(2026, 6, 1), 10.0)], as_of=datetime.date(2026, 6, 1), sector="Technology"
    )
    assert inputs is not None
    assert inputs.lease_liability is None


def test_build_moic_inputs_returns_none_without_price():
    assert (
        build_moic_inputs(PAYLOAD, [], price_observations=[], as_of=datetime.date(2026, 6, 1), sector=None) is None
    )


def test_build_moic_inputs_returns_none_when_nothing_is_disclosed_yet():
    """最初の決算が開示される前の評価日では入力を組み立てられない。"""
    assert (
        build_moic_inputs(PAYLOAD, [], price_observations=[(datetime.date(2021, 1, 1), 10.0)], as_of=datetime.date(2021, 1, 1), sector=None) is None
    )


def test_dilution_uses_the_annual_series_when_it_spans_longer():
    """27.9:年次系列(5期=4年)は日次観測の窓より長いことが多い。"""
    daily = _points([("2025-01-01", 13.5), ("2026-01-01", 14.0)])  # 1年しかない
    inputs = build_moic_inputs(
        PAYLOAD, daily, price_observations=[(datetime.date(2026, 6, 1), 10.0)], as_of=datetime.date(2026, 6, 1), sector="Technology"
    )
    assert inputs is not None
    # 年次:10 → 14 を4年 = 年率 +8.8%
    assert inputs.dilution_cagr == pytest.approx((14 / 10) ** (1 / 4) - 1, rel=1e-2)


# --- 価格ナウキャストの入力(28.3) --------------------------------------------


def test_annualized_log_momentum_is_measured_over_the_actual_elapsed_window():
    """**実測の経過年数で年率換算する**ので、日次でも月次でも値がほぼ揃う。

    これは意図的な設計である(28.3)。ライブは `price_snapshots` の日次全行を、
    バックテストはメモリのために月次サンプルを渡すが、同じ関数を通しても値が
    ぶれないようにしておかないと、「ライブとバックテストで完全に同一のモデルが
    走る」という27.8の保証が崩れる。
    """
    as_of = datetime.date(2026, 6, 1)
    daily = [
        (datetime.date(2025, 6, 2) + datetime.timedelta(days=i), 100.0 * (1.5 ** (i / 364)))
        for i in range(0, 365)
    ]
    monthly = daily[::30]
    if monthly[-1] != daily[-1]:
        monthly.append(daily[-1])
    assert annualized_log_momentum(daily, as_of) == pytest.approx(
        annualized_log_momentum(monthly, as_of), rel=0.05
    )
    assert annualized_log_momentum(daily, as_of) == pytest.approx(math.log(1.5), rel=0.05)


def test_annualized_log_momentum_ignores_prices_after_the_evaluation_date():
    """先読みしないこと。バックテストの正当性そのものが懸かっている。"""
    as_of = datetime.date(2025, 6, 1)
    observations = [
        (datetime.date(2024, 6, 1), 100.0),
        (datetime.date(2025, 6, 1), 150.0),
        (datetime.date(2026, 1, 1), 900.0),  # 評価日より後。使ってはいけない
    ]
    assert annualized_log_momentum(observations, as_of) == pytest.approx(math.log(1.5), rel=1e-3)


def test_annualized_log_momentum_is_none_for_a_too_short_window():
    """上場直後など、窓が半年に満たない銘柄では算出しない(ナウキャストは無効化)。"""
    as_of = datetime.date(2026, 6, 1)
    short = [(datetime.date(2026, 4, 1), 100.0), (datetime.date(2026, 6, 1), 130.0)]
    assert annualized_log_momentum(short, as_of) is None


def test_build_moic_inputs_carries_momentum_when_the_window_allows():
    as_of = datetime.date(2026, 6, 1)
    prices = [
        (datetime.date(2025, 6, 1), 8.0),
        (datetime.date(2025, 12, 1), 9.0),
        (as_of, 10.0),
    ]
    inputs = build_moic_inputs(PAYLOAD, [], price_observations=prices, as_of=as_of, sector="Technology")
    assert inputs is not None
    assert inputs.market_cap == 10.0 * 14.0  # 時価総額は評価日当日の終値から
    assert inputs.log_momentum_12m == pytest.approx(math.log(10.0 / 8.0), rel=1e-3)


# --- 報告通貨と取引通貨の混在(13.5、2026-08-26に発見した欠陥) ---------------


def _payload_in(financial_currency: str, fx_rate: float | None) -> dict:
    """PAYLOAD と同じ会社が、決算だけ別通貨で報告しているケース。"""
    payload = {**PAYLOAD, "info": {**PAYLOAD["info"], "currency": "USD", "financialCurrency": financial_currency}}
    if fx_rate is not None:
        payload["info"]["_fx_rate_financial_to_trading"] = fx_rate
    return payload


def _build(payload):
    return build_moic_inputs(
        payload,
        share_observations=[],
        price_observations=[(datetime.date(2026, 6, 1), 10.0)],
        as_of=datetime.date(2026, 6, 1),
        sector="Technology",
    )


def test_financial_to_trading_rate_is_neutral_when_currencies_match():
    assert financial_to_trading_rate(PAYLOAD) == 1.0
    assert financial_to_trading_rate(_payload_in("USD", None)) == 1.0


def test_statement_amounts_are_converted_to_the_trading_currency():
    """13.5:yfinanceは「株価・時価総額=取引通貨、決算=報告通貨」で返す。

    モデルは `EV = 時価総額 + ネットデット` と `EV / 粗利` を計算するため、
    換算しないと**単位の違う数を足し引きする**。実データでは5,287銘柄中262銘柄
    (5.0%)が該当し、BRL建てで報告するAFYA(fx=0.194)は歪んだEVのまま
    総合17位に載っていた(2026-08-26のランキングで確認)。
    """
    usd = _build(PAYLOAD)
    brl = _build(_payload_in("BRL", 0.20))
    assert usd is not None and brl is not None

    # 株価由来の時価総額は換算しない(もともと取引通貨)
    assert brl.market_cap == pytest.approx(usd.market_cap)
    # 決算由来の金額は fx 倍される
    assert brl.revenue_latest == pytest.approx(usd.revenue_latest * 0.20)
    assert brl.gross_profit_latest == pytest.approx(usd.gross_profit_latest * 0.20)
    assert brl.net_debt == pytest.approx(usd.net_debt * 0.20)
    # 比率で表される入力は通貨に依存しない
    assert brl.gross_margin_latest == pytest.approx(usd.gross_margin_latest)
    assert brl.fcf_margin == pytest.approx(usd.fcf_margin)
    assert brl.equity_to_assets == pytest.approx(usd.equity_to_assets)
    assert brl.revenue_cagr == pytest.approx(usd.revenue_cagr)


def test_unconvertible_currency_mismatch_is_unmeasurable_rather_than_wrong():
    """換算レートが取れないなら「測れない」として Tier 2 へ回す。

    `normalize_financial_currency_value`(ゲート側)の「換算不能は判定不能」と
    同じ方針。誤った単位で計算した順位を出すより、順位を付けないほうが正しい。
    """
    assert financial_to_trading_rate(_payload_in("BRL", None)) is None
    assert _build(_payload_in("BRL", None)) is None
