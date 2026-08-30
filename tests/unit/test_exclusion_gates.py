import datetime

import pytest

from autoscreener.config import UniverseConfig
from autoscreener.screening.exclusion_gates import (
    GateInput,
    annual_share_series_is_comparable,
    compute_altman_z,
    compute_cash_runway_quarters,
    compute_dilution_cagr,
    count_available_quarters,
    dilution_cagr,
    dilution_cagr_with_window,
    evaluate_gates,
    normalize_financial_currency_value,
)


def _universe_config(**overrides) -> UniverseConfig:
    base = dict(
        market="US",
        # 29章:上限は目標倍率の関数になった。既定の目標(10倍)で
        # 3.5B/3B、materialize 範囲(3倍)で 11.7B/10B になる設定。
        exit_market_cap_ceiling_usd=35_000_000_000,
        exit_revenue_ceiling_usd=30_000_000_000,
        min_supported_target_moic=3.0,
        min_price_usd=1.0,
        min_daily_dollar_volume_usd=1_000_000,
        min_listed_quarters=4,
        excluded_sectors=["Financial Services", "Financial", "Real Estate"],
    )
    base.update(overrides)
    return UniverseConfig.model_validate(base)


# --- normalize_financial_currency_value (13.5) ------------------------------


def test_normalize_financial_currency_value_same_currency_unchanged():
    info = {"currency": "USD", "financialCurrency": "USD"}
    assert normalize_financial_currency_value(1000.0, info) == 1000.0


def test_normalize_financial_currency_value_converts_with_fx_rate():
    # HMY実データ相当:ZAR建ての決算値をUSD建てに換算
    info = {"currency": "USD", "financialCurrency": "ZAR", "_fx_rate_financial_to_trading": 0.0625}
    assert normalize_financial_currency_value(81_154_998_272, info) == 81_154_998_272 * 0.0625


def test_normalize_financial_currency_value_missing_fx_rate_returns_none():
    info = {"currency": "USD", "financialCurrency": "ZAR"}
    assert normalize_financial_currency_value(1000.0, info) is None


def test_normalize_financial_currency_value_missing_currency_fields_unchanged():
    info = {}
    assert normalize_financial_currency_value(1000.0, info) == 1000.0


def test_normalize_financial_currency_value_none_input_returns_none():
    info = {"currency": "USD", "financialCurrency": "ZAR", "_fx_rate_financial_to_trading": 0.0625}
    assert normalize_financial_currency_value(None, info) is None


# --- compute_altman_z -------------------------------------------------------


def test_compute_altman_z_uses_latest_period():
    balance_sheet = {
        "Total Assets": {"2023-12-31": 100.0, "2024-12-31": 200.0},
        "Working Capital": {"2023-12-31": 10.0, "2024-12-31": 20.0},
        "Retained Earnings": {"2023-12-31": 5.0, "2024-12-31": 15.0},
        "Stockholders Equity": {"2023-12-31": 50.0, "2024-12-31": 80.0},
        "Total Liabilities Net Minority Interest": {"2023-12-31": 40.0, "2024-12-31": 90.0},
    }
    income_stmt = {"EBIT": {"2023-12-31": 8.0, "2024-12-31": 12.0}}

    z = compute_altman_z(balance_sheet, income_stmt)

    expected = 6.56 * (20 / 200) + 3.26 * (15 / 200) + 6.72 * (12 / 200) + 1.05 * (80 / 90)
    assert z == expected


def test_compute_altman_z_skips_none_and_uses_prior_period():
    balance_sheet = {
        "Total Assets": {"2023-12-31": 100.0, "2024-12-31": None},
        "Working Capital": {"2023-12-31": 10.0, "2024-12-31": None},
        "Retained Earnings": {"2023-12-31": 5.0, "2024-12-31": None},
        "Stockholders Equity": {"2023-12-31": 50.0, "2024-12-31": None},
        "Total Liabilities Net Minority Interest": {"2023-12-31": 40.0, "2024-12-31": None},
    }
    income_stmt = {"EBIT": {"2023-12-31": 8.0, "2024-12-31": None}}

    z = compute_altman_z(balance_sheet, income_stmt)
    assert z is not None  # falls back to the 2023 period rather than returning None


def test_compute_altman_z_missing_data_returns_none():
    assert compute_altman_z({}, {}) is None
    assert compute_altman_z({"Total Assets": {"2024-12-31": 100.0}}, {}) is None


# --- compute_cash_runway_quarters ------------------------------------------


def test_cash_runway_computes_from_recent_burn():
    quarterly_cash_flow = {
        "Free Cash Flow": {
            "2025-06-30": -1000.0,  # oldest of the 5 quarters, should be excluded (only last 4 used)
            "2025-09-30": -20.0,
            "2025-12-31": -30.0,
            "2026-03-31": -40.0,
            "2026-06-30": -10.0,  # most recent quarter
        }
    }
    # avg burn of last 4 (2025-09-30 .. 2026-06-30) = (20+30+40+10)/4 = 25
    runway = compute_cash_runway_quarters(total_cash=100.0, quarterly_cash_flow=quarterly_cash_flow)
    assert runway == 4.0


def test_cash_runway_positive_fcf_is_infinite():
    quarterly_cash_flow = {"Free Cash Flow": {"2026-03-31": 10.0, "2026-06-30": 20.0}}
    runway = compute_cash_runway_quarters(total_cash=100.0, quarterly_cash_flow=quarterly_cash_flow)
    assert runway == float("inf")


def test_cash_runway_missing_data_returns_none():
    assert compute_cash_runway_quarters(None, {"Free Cash Flow": {"2026-01-01": -10.0}}) is None
    assert compute_cash_runway_quarters(100.0, {}) is None


# --- compute_dilution_cagr ---------------------------------------------------


def test_dilution_cagr_computed_correctly():
    # doubling over 3 years = 2^(1/3) - 1 ≈ 25.99%
    cagr = compute_dilution_cagr(100_000_000, 200_000_000, elapsed_years=3.0)
    assert round(cagr, 4) == round(2 ** (1 / 3) - 1, 4)


def test_dilution_cagr_uses_actual_elapsed_years():
    """観測窓が3年でなくても、実測の経過年数で年率換算する(固定1/3にしない)。"""
    over_5y = compute_dilution_cagr(100.0, 200.0, elapsed_years=5.0)
    assert round(over_5y, 4) == round(2 ** (1 / 5) - 1, 4)
    # 同じ倍率でも窓が長いほど年率は小さくなる
    assert over_5y < compute_dilution_cagr(100.0, 200.0, elapsed_years=3.0)


def test_dilution_cagr_missing_data_returns_none():
    assert compute_dilution_cagr(None, 100.0, 3.0) is None
    assert compute_dilution_cagr(100.0, None, 3.0) is None
    assert compute_dilution_cagr(0, 100.0, 3.0) is None
    assert compute_dilution_cagr(100.0, 0, 3.0) is None


def test_dilution_cagr_window_too_short_returns_none():
    assert compute_dilution_cagr(100.0, 200.0, elapsed_years=1.0) is None


def test_dilution_cagr_with_window_skips_missing_share_counts():
    """先頭が株式数Noneでも、非Noneの観測点だけで算出できる(以前は丸ごと欠損)。"""
    start = datetime.date(2023, 1, 2)
    observations = [
        (start, None),
        (start + datetime.timedelta(days=10), 100_000_000.0),
        (start + datetime.timedelta(days=1105), 200_000_000.0),
        (start + datetime.timedelta(days=1110), None),
    ]
    cagr, _window = dilution_cagr_with_window(observations)
    assert cagr is not None
    elapsed = 1095 / 365.25
    assert round(cagr, 6) == round(2 ** (1 / elapsed) - 1, 6)


def test_dilution_cagr_with_window_needs_two_points():
    assert dilution_cagr_with_window([])[0] is None
    assert dilution_cagr_with_window([(datetime.date(2024, 1, 1), 100.0)])[0] is None
    assert dilution_cagr_with_window([(datetime.date(2024, 1, 1), None)])[0] is None


# --- evaluate_gates ----------------------------------------------------------


def _passing_input(**overrides) -> GateInput:
    base = dict(
        market_cap=1_000_000_000,
        total_revenue=500_000_000,
        price=10.0,
        sector="Technology",
        median_daily_dollar_volume=5_000_000,
        dilution_3y_cagr=0.05,
        stockholders_equity=50_000_000,
        cash_runway_quarters=12.0,
        available_quarters=5,
    )
    base.update(overrides)
    return GateInput(**base)


def test_evaluate_gates_all_pass():
    result = evaluate_gates(_passing_input(), _universe_config())
    assert result.passed
    assert result.reasons == []


def test_market_cap_ceiling_excludes():
    """日次バッチのゲートは **materialize 範囲**(最も緩い目標)で切る(29章)。"""
    result = evaluate_gates(_passing_input(market_cap=12_000_000_000), _universe_config())
    assert not result.passed
    assert "market_cap_ceiling" in result.reasons


def test_revenue_ceiling_excludes():
    result = evaluate_gates(_passing_input(total_revenue=11_000_000_000), _universe_config())
    assert "revenue_ceiling" in result.reasons


def test_mid_cap_passes_the_daily_gate_but_not_the_default_target():
    """**29章の要点**:$4Bの企業は日次バッチのゲートを通るが、既定の目標
    (7年で10倍)の母集団には入らない。

    上限を目標倍率の関数にしたことで、この2つは別の判定になった。バッチは
    最も緩い目標(3倍)まで materialize し、目標ごとの絞り込みはAPIが行う
    (`routes._within_target_universe`)。ここでは前者が通り、後者の上限
    (35B/10 = 3.5B)を超えていることを押さえる。
    """
    universe = _universe_config()
    assert evaluate_gates(_passing_input(market_cap=4_000_000_000), universe).passed

    default_target = universe.ceilings_for_target(10.0)
    assert default_target.market_cap_usd == 3_500_000_000
    assert default_target.revenue_usd == 3_000_000_000
    assert 4_000_000_000 >= default_target.market_cap_usd


def test_ceilings_widen_as_the_target_gets_easier():
    """目標が緩いほど上限も緩む——「大きすぎる企業は算数上10倍になれない」
    (15.6)という根拠が10倍という目標に依存しているため(29章)。"""
    universe = _universe_config()
    assert universe.ceilings_for_target(5.0).market_cap_usd == 7_000_000_000
    assert universe.ceilings_for_target(3.0).market_cap_usd == pytest.approx(11_666_666_667, rel=1e-9)


def test_ceilings_stop_widening_at_the_materialised_universe():
    """3倍より緩い目標を指定しても母集団はそれ以上広がらない(29章)。

    黙って狭い母集団を返すと「この目標なら候補が増えるはずなのに増えない」の
    理由が利用者に見えないため、`widening_capped` で明示する。
    """
    universe = _universe_config()
    capped = universe.ceilings_for_target(1.5)
    assert capped.widening_capped is True
    assert capped.market_cap_usd == universe.market_cap_ceiling_usd
    assert capped.target_moic == 3.0
    assert universe.ceilings_for_target(3.0).widening_capped is False


def test_price_floor_excludes():
    result = evaluate_gates(_passing_input(price=0.5), _universe_config())
    assert "price_floor" in result.reasons


def test_excluded_sector_excludes():
    result = evaluate_gates(_passing_input(sector="Real Estate"), _universe_config())
    assert "excluded_sector" in result.reasons


def test_missing_core_fields_excludes_fail_safe():
    result = evaluate_gates(_passing_input(market_cap=None, price=None, sector=None), _universe_config())
    assert "missing_market_cap" in result.reasons
    assert "missing_price" in result.reasons
    assert "missing_sector" in result.reasons


def test_missing_optional_fields_does_not_exclude():
    # 希薄化・自己資本・ランウェイが未算出でも、それ単体では除外しない
    result = evaluate_gates(
        _passing_input(dilution_3y_cagr=None, stockholders_equity=None, cash_runway_quarters=None),
        _universe_config(),
    )
    assert result.passed


def test_dilution_ceiling_excludes():
    result = evaluate_gates(_passing_input(dilution_3y_cagr=0.30), _universe_config())
    assert "dilution_ceiling" in result.reasons


def test_negative_equity_excludes():
    result = evaluate_gates(_passing_input(stockholders_equity=-1.0), _universe_config())
    assert "negative_equity" in result.reasons


def test_zero_or_positive_equity_does_not_exclude():
    result = evaluate_gates(_passing_input(stockholders_equity=0.0), _universe_config())
    assert "negative_equity" not in result.reasons


def test_cash_runway_floor_excludes():
    result = evaluate_gates(_passing_input(cash_runway_quarters=3.0), _universe_config())
    assert "cash_runway_floor" in result.reasons


def test_infinite_cash_runway_passes():
    result = evaluate_gates(_passing_input(cash_runway_quarters=float("inf")), _universe_config())
    assert "cash_runway_floor" not in result.reasons


# --- 上場後最低4四半期(4章・6.2、24.6で実装漏れが判明) ------------------------


def test_count_available_quarters_counts_non_none_periods():
    quarterly_income_stmt = {
        "Total Revenue": {
            "2024-12-31": None,
            "2025-03-31": None,
            "2025-06-30": 100.0,
            "2025-09-30": 110.0,
            "2025-12-31": 120.0,
        }
    }
    assert count_available_quarters(quarterly_income_stmt) == 3


def test_count_available_quarters_missing_field_returns_zero():
    assert count_available_quarters({}) == 0


def test_insufficient_listing_history_excludes():
    result = evaluate_gates(_passing_input(available_quarters=2), _universe_config())
    assert "insufficient_listing_history" in result.reasons


def test_sufficient_listing_history_passes():
    result = evaluate_gates(_passing_input(available_quarters=4), _universe_config())
    assert "insufficient_listing_history" not in result.reasons


# --- 希薄化CAGRのbalance_sheetフォールバック(25.8) --------------------------


def _price_observations(n_years: float, start: float, end: float):
    start_date = datetime.date(2024, 1, 2)
    return [(start_date, start), (start_date + datetime.timedelta(days=int(365.25 * n_years)), end)]


def test_dilution_cagr_prefers_the_longer_observation_window():
    """27.9:実測窓が長いほうを採る。ACTGの誤評価を再現するリグレッションテスト。

    ACTG(Acacia Research)は2022年末の43.5M株から2023年末に99.9M株へ1年で
    株式数が倍増したが、価格ヒストリーの開始が2023-08であり、日次観測の窓には
    その増資が**入っていなかった**。日次観測を無条件に優先していた旧実装では
    希薄化ほぼ0%と測定され、「1株価値の保全」が高評価のまま総合1位に付いていた。

    年次系列(4年窓)のほうが日次観測(3年窓)より長いので、そちらを採るべき。
    """
    obs = _price_observations(3.0, 99_900_000.0, 96_500_000.0)  # 増資後の窓:ほぼ横ばい
    balance_sheet = {
        "Ordinary Shares Number": {
            "2021-12-31": 43_500_000.0,
            "2022-12-31": 43_500_000.0,
            "2023-12-31": 99_900_000.0,
            "2025-12-31": 96_500_000.0,
        }
    }
    from_prices, price_window = dilution_cagr_with_window(obs)
    assert from_prices < 0  # 日次観測だけ見ると「希薄化していない」ように見える

    cagr = dilution_cagr(obs, balance_sheet)
    assert cagr is not None
    # 4年で 43.5M → 96.5M = 年率 +22% 前後。増資が正しく捕まっている。
    assert 0.20 < cagr < 0.24
    assert price_window < 4.0


def test_dilution_cagr_keeps_price_observations_when_they_span_longer():
    """逆に日次観測のほうが長ければそちらを採る(年次は最大5期しか無い)。"""
    obs = _price_observations(6.0, 100.0, 150.0)
    balance_sheet = {"Ordinary Shares Number": {"2022-12-31": 100.0, "2025-12-31": 400.0}}
    elapsed = (obs[-1][0] - obs[0][0]).days / 365.25
    expected = compute_dilution_cagr(100.0, 150.0, elapsed)
    assert abs(dilution_cagr(obs, balance_sheet) - expected) < 1e-12


def test_dilution_cagr_falls_back_to_ordinary_shares_number():
    """price_snapshots側の株式数観測が短すぎて測れない銘柄(実データでは71銘柄、
    スコア5位のMAKOを含む)を、決算書の発行済株式数で回復する。"""
    too_short = _price_observations(0.5, 100.0, 150.0)
    balance_sheet = {
        "Ordinary Shares Number": {"2022-12-31": 100_000_000.0, "2025-12-31": 200_000_000.0}
    }
    cagr = dilution_cagr(too_short, balance_sheet)
    assert cagr is not None
    assert 0.25 < cagr < 0.27  # 3年で2倍 ≒ +26%/年


def test_dilution_cagr_returns_none_when_neither_source_works():
    assert dilution_cagr([], None) is None
    assert dilution_cagr([], {}) is None
    assert dilution_cagr([], {"Ordinary Shares Number": {"2025-12-31": 100.0}}) is None


# --- 年次株式数の単位検証(13.4、2026-08-26に発見した欠陥) --------------------


def _d(text: str) -> datetime.date:
    return datetime.date.fromisoformat(text)


def test_annual_share_series_is_comparable_when_units_match():
    """両系列が同じ単位なら、比は期をまたいでほぼ一定になる(多少の差は許容)。"""
    annual = [(_d("2023-12-31"), 100.0), (_d("2024-12-31"), 110.0)]
    daily = [(_d("2024-01-15"), 101.0), (_d("2025-01-15"), 111.0)]
    assert annual_share_series_is_comparable(annual, daily) is True


def test_annual_share_series_detects_a_reverse_split_unit_mismatch():
    """株式併合(1:20)の実データ型:年次は報告値のまま、日次は現在単位に調整済み。

    比が 0.05 → 1.0 と動く。**実際の増資なら両系列に同じように効くので比は
    動かない**ので、比が動くこと自体が単位不一致の証拠になる。
    修正前はこの系列から「年率−80%の希薄化(=自社株買い)」が読み取られ、
    下限 −5% に丸められた無償の加点になっていた。
    """
    annual = [(_d("2023-12-31"), 2000.0), (_d("2024-12-31"), 110.0)]
    daily = [(_d("2024-01-15"), 100.0), (_d("2025-01-15"), 110.0)]
    assert annual_share_series_is_comparable(annual, daily) is False


def test_dilution_ignores_a_split_contaminated_annual_series():
    """単位が揃っていない年次系列は、たとえ実測窓が長くても採用しない。"""
    daily = [(_d("2023-06-30"), 100.0), (_d("2025-06-30"), 120.0)]
    contaminated = {
        "Ordinary Shares Number": {"2022-12-31": 4000.0, "2023-12-31": 4200.0, "2024-12-31": 115.0}
    }
    # 年次系列を採ると「年率−80%」になるが、日次系列の +9.5%/年 が採用される
    result = dilution_cagr(daily, contaminated)
    assert result is not None and result > 0.0
    assert result == pytest.approx((120.0 / 100.0) ** (1 / 2.0) - 1, rel=0.05)


def test_dilution_still_prefers_a_clean_longer_annual_series():
    """27.9の「長い窓を優先する」挙動は、単位が揃っている限り変えていない。

    ACTG(2023年に株式数が43.5M→99.9Mへ倍増)の回帰:価格ヒストリーの開始が
    増資後だったため日次では希薄化ゼロに見えていた。
    """
    daily = [(_d("2023-08-22"), 99.9), (_d("2025-08-22"), 100.5)]
    clean = {
        "Ordinary Shares Number": {
            "2021-12-31": 43.5,
            "2022-12-31": 43.5,
            "2023-12-31": 99.9,
            "2024-12-31": 100.2,
        }
    }
    assert annual_share_series_is_comparable(
        [(_d("2021-12-31"), 43.5), (_d("2022-12-31"), 43.5), (_d("2023-12-31"), 99.9), (_d("2024-12-31"), 100.2)],
        daily,
    ) is True
    result = dilution_cagr(daily, clean)
    assert result is not None and result > 0.25  # 年率30%の希薄化が検出される
