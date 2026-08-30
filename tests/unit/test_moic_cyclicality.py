"""循環性割引・終端バリュエーション規律・自社株買いの持続性のテスト(30章)。

いずれも「モデルの点推定が、循環のどの局面で観測したかに依存してしまう」
という同じ根に対する処置である。既定値では**挙動が変わらない**こと
(後方互換)も、各項目でテストする——設定を入れて初めて効く形にしておかないと、
過去に測ったKPIとの比較ができなくなるため。
"""

import math

import pytest

from autoscreener.config import load_scoring_config
from autoscreener.scoring.moic import (
    CrossSection,
    MoicInputs,
    _log_multiple_intercept,
    build_cross_section,
    compute_moic,
    damp_growth_for_cyclicality,
    dilution_drag_factor,
    growth_fade_multiple_change,
    residual_reverted_multiple_change,
    terminal_gross_margin,
)
from autoscreener.scoring.point_in_time import series_trend_consistency

NEUTRAL = CrossSection(median_log_momentum=None, median_log_sigma=None, sample_size=1)


@pytest.fixture(scope="module")
def config():
    return load_scoring_config()


def make_inputs(**overrides) -> MoicInputs:
    base = dict(
        market_cap=5.0e8,
        net_debt=0.0,
        revenue_latest=2.0e8,
        gross_profit_latest=1.0e8,
        revenue_cagr=0.25,
        revenue_yoy=0.25,
        revenue_growth_volatility=0.15,
        gross_margin_latest=0.50,
        gross_margin_prior=0.50,
        dilution_cagr=0.0,
        piotroski_ratio=0.6,
        cash_runway_quarters=12.0,
        equity_to_assets=0.5,
        fcf_margin=0.05,
        sector="Technology",
        log_momentum_12m=0.10,
    )
    base.update(overrides)
    return MoicInputs(**base)


def _with(config, path: str, value):
    section, key = path.split(".")
    updated = getattr(config, section).model_copy(update={key: value})
    return config.model_copy(update={section: updated})


# --- 一致度の測定 -------------------------------------------------------------


def test_consistency_is_one_for_a_monotone_series():
    assert series_trend_consistency([0.10, 0.15, 0.20, 0.25]) == pytest.approx(1.0)


def test_consistency_is_zero_for_a_pure_round_trip():
    """上がって同じだけ下がる=正味の変化ゼロ。循環そのもの。"""
    assert series_trend_consistency([0.10, 0.20, 0.10]) == pytest.approx(0.0)


def test_consistency_needs_at_least_three_points():
    assert series_trend_consistency([0.10, 0.20]) is None
    assert series_trend_consistency([0.10, 0.10, 0.10]) is None  # 変化が無い


# --- 粗利率の循環性割引(30.1) ------------------------------------------------


def test_margin_trend_is_damped_when_the_margin_history_oscillates(config):
    """市況で反発しただけの粗利率を7年分の改善として計上しない。

    直近2期の差分(+5pt)は同じでも、履歴が上下に振れている銘柄では外挿量が縮む。
    """
    cfg = _with(config, "margin.cyclicality_damping", 1.0)
    structural = make_inputs(
        gross_margin_latest=0.30, gross_margin_prior=0.25, gross_margin_consistency=1.0
    )
    cyclical = make_inputs(
        gross_margin_latest=0.30, gross_margin_prior=0.25, gross_margin_consistency=0.0
    )
    assert terminal_gross_margin(structural, cfg) > terminal_gross_margin(cyclical, cfg)
    # 一致度0なら外挿はゼロ=現状維持。
    assert terminal_gross_margin(cyclical, cfg) == pytest.approx(0.30)


def test_margin_cyclicality_damping_is_symmetric(config):
    """悪化方向の一時的な落ち込みも同じだけ割り引く(片側だけの加点装置にしない)。"""
    cfg = _with(config, "margin.cyclicality_damping", 1.0)
    cyclical = make_inputs(
        gross_margin_latest=0.25, gross_margin_prior=0.30, gross_margin_consistency=0.0
    )
    assert terminal_gross_margin(cyclical, cfg) == pytest.approx(0.25)


def test_margin_damping_is_skipped_when_consistency_is_unmeasurable(config):
    """27.1:欠損を減点に読み替えない。年次が3期未満の銘柄は補正しない。"""
    cfg = _with(config, "margin.cyclicality_damping", 1.0)
    off = _with(config, "margin.cyclicality_damping", 0.0)
    inputs = make_inputs(
        gross_margin_latest=0.30, gross_margin_prior=0.25, gross_margin_consistency=None
    )
    assert terminal_gross_margin(inputs, cfg) == terminal_gross_margin(inputs, off)


# --- 成長率の循環性割引(30.1) ------------------------------------------------


def test_growth_is_pulled_toward_the_terminal_rate_for_cyclical_revenue(config):
    cfg = _with(config, "growth.cyclicality_damping", 1.0)
    cyclical = make_inputs(revenue_trend_consistency=0.0)
    assert damp_growth_for_cyclicality(0.40, cyclical, cfg) == pytest.approx(
        cfg.growth.terminal_rate
    )
    structural = make_inputs(revenue_trend_consistency=1.0)
    assert damp_growth_for_cyclicality(0.40, structural, cfg) == pytest.approx(0.40)


def test_growth_damping_can_be_switched_off(config):
    cfg = _with(config, "growth.cyclicality_damping", 0.0)
    inputs = make_inputs(revenue_trend_consistency=0.0)
    assert damp_growth_for_cyclicality(0.40, inputs, cfg) == pytest.approx(0.40)


def test_growth_damping_is_skipped_when_consistency_is_unmeasurable(config):
    """27.1:年次3期未満で一致度が測れない銘柄は補正しない。"""
    inputs = make_inputs(revenue_trend_consistency=None)
    assert damp_growth_for_cyclicality(0.40, inputs, config) == pytest.approx(0.40)


# --- 自社株買いの持続性(30.4) ------------------------------------------------


def test_buyback_decays_but_dilution_persists(config):
    cfg = _with(config, "dilution.buyback_persistence", 0.7)
    horizon = cfg.horizon_years
    # 増資側は従来どおり複利。
    assert dilution_drag_factor(0.10, cfg) == pytest.approx(1.10**horizon)
    # 自社株買い側は減衰するので、複利より 1 に近い(=無償の倍率が減る)。
    faded = dilution_drag_factor(-0.05, cfg)
    assert faded > 0.95**horizon
    assert faded < 1.0


def test_buyback_persistence_one_reproduces_compounding(config):
    cfg = _with(config, "dilution.buyback_persistence", 1.0)
    assert dilution_drag_factor(-0.05, cfg) == pytest.approx(0.95**cfg.horizon_years)


def test_buyback_fade_lowers_expected_moic(config):
    """下限に張り付いた自社株買い銘柄が受け取っていた 1.43 倍が縮む。"""
    inputs = make_inputs(dilution_cagr=-0.05)
    base = compute_moic(inputs, NEUTRAL, _with(config, "dilution.buyback_persistence", 1.0))
    faded = compute_moic(inputs, NEUTRAL, config)  # 出荷設定は減衰あり
    assert base is not None and faded is not None
    assert faded.expected_moic < base.expected_moic


# --- 終端 EV/粗利の上限(30.3) ------------------------------------------------


def _cross_section_inputs(n: int = 40) -> list[MoicInputs]:
    """EV/粗利が 2 倍〜(2+n)倍に並ぶ合成断面。"""
    return [
        make_inputs(market_cap=1.0e8 * (2 + i), gross_profit_latest=1.0e8, revenue_latest=2.0e8)
        for i in range(n)
    ]


def test_terminal_multiple_cap_binds_only_on_the_expensive_tail(config):
    cfg = _with(
        _with(config, "multiple.terminal_cap_percentile", 0.95),
        "multiple.terminal_cap_slack",
        1.0,
    )
    cross = build_cross_section(_cross_section_inputs(), cfg)
    assert cross.ev_to_gross_profit_cap is not None
    cheap = make_inputs(market_cap=5.0e8, gross_profit_latest=1.0e8)
    expensive = make_inputs(market_cap=1.0e10, gross_profit_latest=1.0e8)
    assert compute_moic(cheap, cross, cfg).terminal_multiple_capped is False
    # 上限に当たった銘柄は期待倍率が大きく削られてランキングから外れうるので、
    # ここでは 27.17 の足切りを外して「上限が効いたこと」だけを見る。
    capped = compute_moic(expensive, cross, cfg, enforce_min_expected_moic=False)
    assert capped.terminal_multiple_capped is True
    assert capped.target_ev_to_gross_profit == pytest.approx(cross.ev_to_gross_profit_cap)


def test_terminal_multiple_cap_can_be_switched_off(config):
    cfg = _with(config, "multiple.terminal_cap_percentile", 0.0)
    cross = build_cross_section(_cross_section_inputs(), cfg)
    assert cross.ev_to_gross_profit_cap is None


def test_terminal_multiple_cap_needs_a_thick_cross_section(config):
    """断面が薄いと分位点が不安定なので上限を作らない。"""
    cfg = _with(config, "multiple.terminal_cap_percentile", 0.95)
    assert build_cross_section(_cross_section_inputs(5), cfg).ev_to_gross_profit_cap is None


# --- 成長調整後の割高・割安(30.5) --------------------------------------------


def test_residual_reversion_is_inert_by_default(config):
    """30.5 は測定のうえで**採用しなかった**。出荷設定では効いてはいけない。"""
    assert config.multiple.residual_reversion_weight == 0.0
    cross = CrossSection(log_multiple_intercept=1.5)
    assert residual_reverted_multiple_change(20.0, 0.30, 0.05, cross, config) == pytest.approx(
        growth_fade_multiple_change(0.30, 0.05, config)
    )


def test_residual_reversion_penalises_the_expensive_and_credits_the_cheap(config):
    """**同じ成長率**で比べて割高な銘柄だけが圧縮され、割安な銘柄だけが戻る。"""
    cfg = _with(config, "multiple.residual_reversion_weight", 0.30)
    cross = CrossSection(log_multiple_intercept=1.5)
    fair = math.exp(1.5 + cfg.multiple.growth_elasticity * 0.30)
    plain = growth_fade_multiple_change(0.30, 0.05, cfg)
    assert residual_reverted_multiple_change(fair, 0.30, 0.05, cross, cfg) == pytest.approx(plain)
    assert residual_reverted_multiple_change(fair * 3, 0.30, 0.05, cross, cfg) < plain
    assert residual_reverted_multiple_change(fair / 3, 0.30, 0.05, cross, cfg) > plain


def test_residual_reversion_does_not_reward_low_growth_cheapness(config):
    """v3 が踏んだ罠(万年割安株の買い上げ)を再現しないこと。

    成長率が低い銘柄は寄せる先(c + κ·g)も低いので、単に倍率が低いだけでは
    リレーティングを受け取れない。
    """
    cfg = _with(config, "multiple.residual_reversion_weight", 0.30)
    cross = CrossSection(log_multiple_intercept=1.5)
    kappa = cfg.multiple.growth_elasticity
    low_growth_fair = math.exp(1.5 + kappa * 0.0)
    assert residual_reverted_multiple_change(
        low_growth_fair, 0.0, 0.03, cross, cfg
    ) == pytest.approx(growth_fade_multiple_change(0.0, 0.03, cfg))


def test_intercept_is_none_when_reversion_is_disabled(config):
    assert _log_multiple_intercept(_cross_section_inputs(), config) is None


# --- 出荷設定が実際に有効になっていること -------------------------------------


def test_shipped_config_enables_the_cyclicality_corrections(config):
    """S-7 で踏んだ「docstringは主張しているが設定が無効」の再発を防ぐ。"""
    assert config.growth.cyclicality_damping > 0
    assert config.margin.cyclicality_damping > 0
    assert config.dilution.buyback_persistence < 1.0
    assert config.multiple.terminal_cap_percentile > 0


def test_intercept_is_measured_from_the_cross_section(config):
    cfg = _with(config, "multiple.residual_reversion_weight", 0.30)
    intercept = _log_multiple_intercept(_cross_section_inputs(), cfg)
    assert intercept is not None
    # 合成断面は EV/粗利 2〜41 倍、成長率は全銘柄 0.25 で共通。
    assert intercept == pytest.approx(
        math.log(21.5) - cfg.multiple.growth_elasticity * 0.25, abs=0.1
    )


# --- 30章を全部切ったときに v4 と一致すること ---------------------------------


def test_all_switches_off_reproduces_v4(config):
    """30章の各項目は**設定でオン/オフできる**。全部切れば v4 と同一の出力になる。

    出荷設定を変えたときに「何が v4 からの差分なのか」を切り分けられるように、
    この経路をテストで固定しておく。
    """
    off = config
    for path in (
        "growth.cyclicality_damping",
        "margin.cyclicality_damping",
        "multiple.terminal_cap_percentile",
        "multiple.residual_reversion_weight",
    ):
        off = _with(off, path, 0.0)
    off = _with(off, "dilution.buyback_persistence", 1.0)

    inputs = make_inputs(
        gross_margin_consistency=0.0,
        revenue_trend_consistency=0.0,
        dilution_cagr=-0.05,
    )
    result = compute_moic(inputs, NEUTRAL, off)
    assert result is not None
    assert result.growth_cyclicality_adjustment == pytest.approx(0.0)
    assert result.statement_growth_rate == pytest.approx(result.base_growth_rate)
    assert result.terminal_multiple_capped is False
    assert result.dilution_drag == pytest.approx(0.95**off.horizon_years)


def test_shipped_config_damps_a_cyclical_stock(config):
    """出荷設定で、循環銘柄の期待倍率が構造的に伸びている銘柄より低くなること。"""
    cyclical = make_inputs(
        gross_margin_latest=0.30,
        gross_margin_prior=0.25,
        gross_margin_consistency=0.0,
        revenue_trend_consistency=0.2,
    )
    structural = make_inputs(
        gross_margin_latest=0.30,
        gross_margin_prior=0.25,
        gross_margin_consistency=1.0,
        revenue_trend_consistency=1.0,
    )
    assert compute_moic(cyclical, NEUTRAL, config).expected_moic < compute_moic(
        structural, NEUTRAL, config
    ).expected_moic
