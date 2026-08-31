"""実現時価総額倍率モデル v4 のテスト(27章・28章)。

**このファイルの多くは「かつて実データで踏んだ論理破綻」のリグレッションテスト**
である。10バガー探索モデルの失敗は例外を投げずに静かに順位を壊すので、
壊れ方そのものをテストで固定しておかないと再発に気づけない。
"""

import math

import pytest

from autoscreener.config import load_scoring_config
from autoscreener.scoring.moic import (
    CrossSection,
    MoicInputs,
    base_initial_growth,
    build_cross_section,
    compute_moic,
    growth_fade,
    growth_fade_multiple_change,
    growth_path,
    health_index,
    is_initial_growth_clamped,
    moic_quantiles,
    nowcast_initial_growth,
    raw_log_moic_sigma,
    shrink_log_moic_sigma,
    survival_probability,
    terminal_gross_margin,
)


@pytest.fixture(scope="module")
def config():
    return load_scoring_config()


# ナウキャストも σ の縮小も無効になる中立の断面。因子を1つずつ検証するときは
# これを使い、断面依存の効果は専用のテストで見る。
NEUTRAL = CrossSection(median_log_momentum=None, median_log_sigma=None, sample_size=1)


def make_inputs(**overrides) -> MoicInputs:
    """中庸な成長企業。テストごとに1因子だけ動かす。"""
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


# --- 初期成長率 ---------------------------------------------------------------


def test_initial_growth_takes_the_slower_of_two_measurements(config):
    """27.13:CAGRとYoYが食い違うときは遅いほうを信じる。

    IMMRは買収による連結で3年CAGRが+214%になっていたが直近YoYは+11%だった。
    食い違いは「少なくとも一方が成長の実力を表していない」ことの徴候であり、
    7年間の外挿という用途では安全側へ倒すのが正しい。
    """
    inputs = make_inputs(revenue_cagr=2.14, revenue_yoy=0.11)
    assert base_initial_growth(inputs, config) == pytest.approx(0.11)


def test_initial_growth_is_clamped_to_a_survivable_range(config):
    assert base_initial_growth(make_inputs(revenue_cagr=5.0, revenue_yoy=5.0), config) == pytest.approx(
        config.growth.max_initial_rate
    )
    assert base_initial_growth(make_inputs(revenue_cagr=-0.9, revenue_yoy=-0.9), config) == pytest.approx(
        config.growth.min_initial_rate
    )


def test_initial_growth_falls_back_to_whichever_measurement_exists(config):
    assert base_initial_growth(make_inputs(revenue_cagr=None, revenue_yoy=0.3), config) == pytest.approx(0.3)
    assert base_initial_growth(make_inputs(revenue_cagr=0.2, revenue_yoy=None), config) == pytest.approx(0.2)
    assert base_initial_growth(make_inputs(revenue_cagr=None, revenue_yoy=None), config) is None


def _with_single_observation_cap(config, value: float):
    """S-7の上限だけを差し替えた設定を作る(挙動そのものを検証するため)。"""
    return config.model_copy(
        update={"growth": config.growth.model_copy(update={"max_initial_rate_single_observation": value})}
    )


def test_single_observation_growth_uses_a_more_conservative_ceiling(config):
    """S-7(docs/model_audit_v4_2026-08-26.md): 27.13の「食い違ったら遅いほうを信じる」
    安全装置は、CAGR・YoYの両方が揃っている銘柄でしか働かない。片方しか無い
    銘柄(BRUN型:上場直後で3年CAGRが無い)は、検証されないまま上限まで
    採用されてしまうため、より保守的な上限を使う。

    **2026-08-26修正**:このテストは以前 `single <= max_initial_rate_single_observation`
    しか見ておらず、設定値が既定の 1.0(=上限として機能しない)のままだと
    **常に成立する空虚なアサーション**だった。実際 `config/scoring.yaml` に
    この項目が書かれておらず、S-7は本番設定で丸ごと無効なのにテストは緑だった。
    設定値を明示的に差し替えて、上限が**効いていること**を検証する。
    """
    capped = _with_single_observation_cap(config, 0.40)
    single = base_initial_growth(make_inputs(revenue_cagr=None, revenue_yoy=5.0), capped)
    both = base_initial_growth(make_inputs(revenue_cagr=5.0, revenue_yoy=5.0), capped)
    assert single == pytest.approx(0.40)
    assert both == pytest.approx(capped.growth.max_initial_rate)
    assert single < both


def test_shipped_config_actually_enables_the_single_observation_ceiling(config):
    """本番設定でS-7が有効であること。無効(=`max_initial_rate` 以上)なら、
    モデルのdocstringが主張する安全装置が存在しないことになる。"""
    assert config.growth.max_initial_rate_single_observation < config.growth.max_initial_rate


def test_nowcast_cannot_bypass_the_single_observation_ceiling(config):
    """S-7の迂回路(2026-08-26に発見)。

    観測が1つしかない銘柄の上限を `base_initial_growth` にだけ掛けても、
    価格ナウキャストが素の `max_initial_rate` まで丸めていたため、補正を
    経由すれば保守的な上限を超えて戻れてしまった。両者は同じ上限を見る。
    """
    capped = _with_single_observation_cap(config, 0.40)
    cross_section = CrossSection(median_log_momentum=0.0, median_log_sigma=None, sample_size=500)
    single_obs = make_inputs(revenue_cagr=None, revenue_yoy=0.38, log_momentum_12m=5.0)
    adjusted, _ = nowcast_initial_growth(0.38, single_obs, cross_section, capped)
    assert adjusted <= 0.40 + 1e-9

    both_obs = make_inputs(revenue_cagr=0.38, revenue_yoy=0.38, log_momentum_12m=5.0)
    adjusted_both, _ = nowcast_initial_growth(0.38, both_obs, cross_section, capped)
    assert adjusted_both > 0.40


def test_growth_rate_clamped_flag_detects_ceiling_and_floor(config):
    """S-6: 成長率が上限/下限に張り付いた銘柄をフラグで区別できること。
    上限に当たった銘柄は「成長力を測れた」のではなく「外挿範囲を超えて
    丸められた」状態であり、実データでは上位30の17%が該当した。
    """
    assert is_initial_growth_clamped(make_inputs(revenue_cagr=5.0, revenue_yoy=5.0), config) is True
    assert is_initial_growth_clamped(make_inputs(revenue_cagr=-0.9, revenue_yoy=-0.9), config) is True
    assert is_initial_growth_clamped(make_inputs(revenue_cagr=0.20, revenue_yoy=0.20), config) is False


# --- 価格ナウキャスト(28.3) --------------------------------------------------


def test_nowcast_uses_the_excess_over_the_cross_section(config):
    """28.3:市場全体の動きではなく、それに対する**超過分**だけを使う。

    絶対リターンを使うと強気相場では全銘柄が一律に上方修正され、順位に何の
    情報も加わらないまま確率の水準だけが上がる。
    """
    cross_section = CrossSection(median_log_momentum=0.30, median_log_sigma=None, sample_size=500)
    # 市場中央値と同じだけ上がった銘柄は、まったく補正されない
    at_median = make_inputs(log_momentum_12m=0.30)
    adjusted, delta = nowcast_initial_growth(0.25, at_median, cross_section, config)
    assert delta == pytest.approx(0.0)
    assert adjusted == pytest.approx(0.25)

    # 市場を上回った銘柄は上方修正、下回った銘柄は下方修正
    outperformer = make_inputs(log_momentum_12m=0.60)
    laggard = make_inputs(log_momentum_12m=0.00)
    assert nowcast_initial_growth(0.25, outperformer, cross_section, config)[1] > 0
    assert nowcast_initial_growth(0.25, laggard, cross_section, config)[1] < 0


def test_nowcast_is_bounded(config):
    """どれだけ株価が動いても、成長率推定の修正は `nowcast_cap` を超えない。

    株価は成長期待以外の理由(リスクプレミアム・流動性・センチメント)でも動く。
    上限を置かないと、投機的に10倍になった銘柄の成長率を無制限に上方修正して
    しまい、恒等式モデルがモメンタム戦略に化ける。
    """
    cross_section = CrossSection(median_log_momentum=0.0, median_log_sigma=None, sample_size=500)
    moonshot = make_inputs(log_momentum_12m=5.0)
    _, delta = nowcast_initial_growth(0.25, moonshot, cross_section, config)
    assert delta == pytest.approx(config.growth.nowcast_cap)


def test_nowcast_is_skipped_when_momentum_is_unavailable(config):
    """モメンタムが測れない銘柄(上場直後など)は補正0。欠損を減点に読み替えない。"""
    cross_section = CrossSection(median_log_momentum=0.10, median_log_sigma=None, sample_size=500)
    blank = make_inputs(log_momentum_12m=None)
    assert nowcast_initial_growth(0.25, blank, cross_section, config) == (0.25, 0.0)


def test_nowcast_sign_flip_uses_the_narrower_cap(config):
    """S-8(docs/model_audit_v4_2026-08-26.md): 決算ベースの成長が負(縮小)なのに
    株価トレンドで成長側へ反転させる補正は、一次情報(決算)を株価で上書き
    する行為であり、通常の nowcast_cap より狭い nowcast_cap_sign_flip までしか
    動かしてはならない(ALTO型:決算-11.7%が株価補正で+3.3%に反転していた)。

    **2026-08-26修正**:本番設定の `nowcast_cap_sign_flip` は 1.0 = 事実上無効
    (実測でKPIを改善しなかったため意図的にそうしてある。config/scoring.yaml参照)。
    以前のアサーションは `delta <= 1.0` を見ているだけで、機構が働いていても
    いなくても通る空虚なテストだった。**機構そのもの**を検証するため、
    狭い上限を明示的に設定して比較する。
    """
    narrow = config.model_copy(
        update={"growth": config.growth.model_copy(update={"nowcast_cap_sign_flip": 0.05})}
    )
    cross_section = CrossSection(median_log_momentum=0.0, median_log_sigma=None, sample_size=500)
    strong_outperformer = make_inputs(log_momentum_12m=5.0)

    _, flipping = nowcast_initial_growth(-0.10, strong_outperformer, cross_section, narrow)
    assert flipping == pytest.approx(0.05)

    # 反転しない(元から正)なら通常の nowcast_cap まで動いてよい
    _, not_flipping = nowcast_initial_growth(0.10, strong_outperformer, cross_section, narrow)
    assert not_flipping == pytest.approx(narrow.growth.nowcast_cap)


def test_nowcast_does_not_narrow_the_cap_when_growth_stays_negative(config):
    """S-8: 反転しない(下方修正のまま、または decisive でない)補正は通常どおり
    nowcast_cap まで動いてよい。狭い上限は「符号を反転させる」ときだけ働く。"""
    cross_section = CrossSection(median_log_momentum=5.0, median_log_sigma=None, sample_size=500)
    laggard = make_inputs(log_momentum_12m=0.0)
    _, delta = nowcast_initial_growth(-0.10, laggard, cross_section, config)
    assert delta == pytest.approx(-config.growth.nowcast_cap)


# --- 成長の減衰と質(28.10) ---------------------------------------------------


def test_growth_path_decays_toward_the_terminal_rate(config):
    path = growth_path(0.60, config.growth.fade, config)
    assert len(path) == config.horizon_years
    assert path[0] > path[-1] > config.growth.terminal_rate
    assert all(earlier >= later for earlier, later in zip(path, path[1:]))


def test_quality_makes_growth_last_longer(config):
    """28.10:F-scoreが高い企業ほど超過成長がゆっくり消える。"""
    strong = growth_fade(make_inputs(piotroski_ratio=1.0), config)
    weak = growth_fade(make_inputs(piotroski_ratio=0.0), config)
    assert strong > config.growth.fade > weak
    assert config.growth.min_fade <= weak < strong <= config.growth.max_fade


def test_quality_is_neutral_when_the_f_score_is_unavailable(config):
    assert growth_fade(make_inputs(piotroski_ratio=None), config) == config.growth.fade


# --- 粗利率の外挿(15.1②) - S-1/S-2リグレッション(docs/model_audit_v4_2026-08-26.md) ---


def test_margin_floor_does_not_lift_a_collapsing_margin(config):
    """S-1: 粗利率が崩壊している銘柄で floor が改善に反転しないこと。

    AMR(2026-08-25、v4ランキング6位)の実データ:粗利率 11.21%→1.17% の崩壊に
    対し、旧実装は floor=0.05 が下から押し上げて margin_multiple 4.29倍を
    返していた。floor は現在値より上へ持ち上げてはならない。
    """
    inputs = make_inputs(gross_margin_latest=0.0117, gross_margin_prior=0.1121)
    terminal = terminal_gross_margin(inputs, config)
    assert terminal <= inputs.gross_margin_latest


def test_margin_floor_does_not_lift_a_margin_without_a_prior_period(config):
    """S-1の修正漏れ(2026-08-26に発見)。

    S-1は「floor を現在値より上へ持ち上げない」よう直したが、**前期の粗利率が
    取れない銘柄が通る early return** は `_clamp(current, floor, ceiling)` の
    ままだった。粗利率1.2%の銘柄はトレンドが測れないだけで終端5.0%(=floor)へ
    持ち上げられ、`margin_multiple` 4.3倍という無償の加点を受けていた——
    S-1が塞いだはずの穴が、もう一方の経路に残っていた。
    """
    no_prior = make_inputs(gross_margin_latest=0.012, gross_margin_prior=None)
    assert terminal_gross_margin(no_prior, config) == pytest.approx(0.012)

    # floor より上の銘柄は従来どおり(現状維持)
    healthy = make_inputs(gross_margin_latest=0.42, gross_margin_prior=None)
    assert terminal_gross_margin(healthy, config) == pytest.approx(0.42)

    # 上限は引き続き効く
    absurd = make_inputs(gross_margin_latest=0.99, gross_margin_prior=None)
    assert terminal_gross_margin(absurd, config) == pytest.approx(config.margin.ceiling)


def test_margin_relative_cap_limits_thin_margin_expansion(config):
    """S-2: 粗利率が薄い銘柄では絶対ポイントの上限が過大な相対倍率になる。

    ALTO(2026-08-25、4位)型:3.8%→11.6%(絶対+7.8pt)は3.06倍の改善だった。
    相対上限を掛けたら、その倍率を超えないこと。
    """
    thin = make_inputs(gross_margin_latest=0.038, gross_margin_prior=0.0101)
    terminal = terminal_gross_margin(thin, config)
    assert terminal / thin.gross_margin_latest <= config.margin.max_relative_change


def test_margin_extrapolation_still_moves_normally_within_bounds(config):
    """S-1/S-2の修正が、通常の(フロア・相対上限に当たらない)ケースの挙動を
    変えていないことを確認する回帰テスト。"""
    improving = make_inputs(gross_margin_latest=0.55, gross_margin_prior=0.50)
    terminal = terminal_gross_margin(improving, config)
    assert terminal > improving.gross_margin_latest
    assert terminal <= improving.gross_margin_latest + config.margin.max_total_change


def test_margin_extrapolation_depends_only_on_the_two_most_recent_periods(config):
    """S-3/S-4を試して不採用にした経緯のリグレッション(docs/model_audit_v4_2026-08-26.md §11)。

    粗利率の年次系列を使う案を2件実装して `run-backtest` で比較し、どちらも
    採らなかった:
      S-3(全期間の最小二乗の傾き) → デシル単調性 0.842→0.745 に悪化
      S-4(EV/粗利の分母を履歴中央値で正規化)→ 順位指標は改善したが確率の
          水準を壊した(上位5銘柄の平均 5.0%→13.3%)。粗利率改善の二重計上。

    したがって粗利率の外挿は `gross_margin_latest` と `gross_margin_prior` の
    2点だけで決まる。**この2点が同じなら結果も同じ**であることを固定して、
    年次系列を使う実装が無自覚に復活するのを防ぐ。
    """
    a = make_inputs(gross_margin_latest=0.038, gross_margin_prior=0.0101)
    b = make_inputs(gross_margin_latest=0.038, gross_margin_prior=0.0101, revenue_cagr=0.05, revenue_yoy=0.05)
    assert terminal_gross_margin(a, config) == pytest.approx(terminal_gross_margin(b, config))


# --- 終端マルチプル(28.2) ----------------------------------------------------


def test_growth_compression_removes_the_price_already_paid_for_growth(config):
    """28.2の中核。**高成長銘柄ほどマルチプルは圧縮される。**

    現在の倍率は「今の成長率の企業」への値づけである。モデルが7年かけて成長を
    減速させる以上、その分だけ倍率が下がるのが整合的であり、そうしないと
    成長の対価を二重に受け取ることになる。
    """
    fast = growth_fade_multiple_change(0.50, 0.09, config)
    slow = growth_fade_multiple_change(0.10, 0.04, config)
    assert fast < slow < 1.0


def test_no_free_rerating_for_cheap_stocks(config):
    """v3で最も害の大きかった経路のリグレッションテスト(28.2)。

    v3は現在のEV/粗利をセクター中央値へ50%寄せていた。実測ではこの項の順位IC
    が −0.023(t=−3.1、9評価日中7日で負)——順位を**悪化**させていた。機序は
    「分子は今日のEV・分母は最大15ヶ月前の粗利」という不整合にあり、業績悪化が
    まだ決算に出ていない銘柄が「割安」に見えて買い上げられていた。

    v4のマルチプル変化は成長率だけの関数であり、**現在の倍率の水準に一切
    依存しない**。同じ成長率なら、割安な銘柄も割高な銘柄も同じ倍率変化になる。
    """
    cheap = compute_moic(
        make_inputs(market_cap=1.0e8, revenue_latest=2.0e8, gross_profit_latest=1.0e8), NEUTRAL, config
    )
    expensive = compute_moic(
        make_inputs(market_cap=3.0e9, revenue_latest=2.0e8, gross_profit_latest=1.0e8), NEUTRAL, config
    )
    assert cheap is not None and expensive is not None
    assert cheap.multiple_change == pytest.approx(expensive.multiple_change)


def test_multiple_change_is_independent_of_sector(config):
    """v4はセクター中央値を参照しない。セクターを変えても倍率変化は動かない。"""
    tech = compute_moic(make_inputs(sector="Technology"), NEUTRAL, config)
    energy = compute_moic(make_inputs(sector="Energy"), NEUTRAL, config)
    assert tech is not None and energy is not None
    assert tech.multiple_change == pytest.approx(energy.multiple_change)
    assert tech.probability == pytest.approx(energy.probability)


# --- 生存確率 -----------------------------------------------------------------


def test_health_index_spans_the_full_range():
    strong = make_inputs(
        piotroski_ratio=1.0, cash_runway_quarters=math.inf, equity_to_assets=0.9, fcf_margin=0.4
    )
    weak = make_inputs(piotroski_ratio=0.0, cash_runway_quarters=1.0, equity_to_assets=0.0, fcf_margin=-0.5)
    assert health_index(strong) == pytest.approx(1.0)
    assert health_index(weak) == pytest.approx(-1.0)


def test_health_index_is_neutral_when_nothing_is_computable():
    """算出できる項目が1つも無ければ中立。欠損を減点に読み替えない(27章の方針)。"""
    blank = make_inputs(
        piotroski_ratio=None, cash_runway_quarters=None, equity_to_assets=None, fcf_margin=None
    )
    assert health_index(blank) == 0.0


def test_survival_probability_is_monotonic_in_health(config):
    assert survival_probability(1.0, config) > survival_probability(0.0, config)
    assert survival_probability(0.0, config) > survival_probability(-1.0, config)
    assert 0.0 < survival_probability(-1.0, config) < 1.0


# --- 不確実性 -----------------------------------------------------------------


def test_leverage_widens_the_distribution(config):
    """27.13:EVの変動が株主価値に増幅されて伝わるなら、ばらつきも増幅される。

    これを入れないと、時価総額がEVのわずか数%しかない銘柄(CRMT)を
    低リスク銘柄と同じばらつきで評価し、P(10x)=75%で1位に据えてしまう。
    """
    inputs = make_inputs()
    rates = growth_path(0.25, config.growth.fade, config)
    high = raw_log_moic_sigma(inputs, rates, config.growth.fade, 3.0, 0.0, config)
    low = raw_log_moic_sigma(inputs, rates, config.growth.fade, 1.0, 0.0, config)
    assert high > low


def test_unstable_growth_widens_the_distribution(config):
    rates = growth_path(0.25, config.growth.fade, config)
    stable = raw_log_moic_sigma(
        make_inputs(revenue_growth_volatility=0.10), rates, config.growth.fade, 1.0, 0.0, config
    )
    unstable = raw_log_moic_sigma(
        make_inputs(revenue_growth_volatility=0.60), rates, config.growth.fade, 1.0, 0.0, config
    )
    assert unstable > stable


def test_financial_fragility_widens_the_distribution(config):
    """28.6:脆弱な企業は上下どちらの裾も厚い。

    実測では健全性が最下位のデシルほど「1年で2倍」も「−50%以下」も同時に多い。
    生存確率(下向き)だけを掛けて σ(両側)を据え置くと、上向きの裾を系統的に
    過小評価することになる。
    """
    rates = growth_path(0.25, config.growth.fade, config)
    fragile = raw_log_moic_sigma(make_inputs(), rates, config.growth.fade, 1.0, -1.0, config)
    solid = raw_log_moic_sigma(make_inputs(), rates, config.growth.fade, 1.0, 1.0, config)
    assert fragile > solid


def test_sigma_shrinks_toward_the_cross_section(config):
    """28.4:σ の断面のばらつきは大半がデータ充足の差なので、中心へ寄せる。"""
    cross_section = CrossSection(median_log_momentum=None, median_log_sigma=1.0, sample_size=500)
    wide = shrink_log_moic_sigma(2.0, cross_section, config)
    narrow = shrink_log_moic_sigma(0.6, cross_section, config)
    # 縮小後も順序は保たれるが、差は大きく縮む
    assert narrow < wide
    assert (wide - narrow) < (2.0 - 0.6)
    # 中心と一致する σ は動かない
    assert shrink_log_moic_sigma(1.0, cross_section, config) == pytest.approx(1.0)


def test_sigma_is_untouched_without_a_cross_section_centre(config):
    assert shrink_log_moic_sigma(1.2, NEUTRAL, config) == pytest.approx(1.2)


# --- クロスセクション(28.5) --------------------------------------------------


def test_cross_section_summarises_the_universe(config):
    universe = [
        make_inputs(log_momentum_12m=m, revenue_growth_volatility=v)
        for m, v in ((-0.4, 0.10), (0.0, 0.20), (0.1, 0.30), (0.5, 0.45), (1.2, 0.60))
    ]
    cross_section = build_cross_section(universe, config)
    assert cross_section.sample_size == 5
    assert cross_section.median_log_momentum == pytest.approx(0.1)
    assert cross_section.median_log_sigma is not None
    assert config.uncertainty.min_total_sigma <= cross_section.median_log_sigma <= config.uncertainty.max_total_sigma


def test_cross_section_round_trips_through_json():
    """`scores.inputs` に保存して、APIが任意ホライズンで復元できること(27.24)。"""
    original = CrossSection(median_log_momentum=0.12, median_log_sigma=0.98, sample_size=712)
    assert CrossSection.from_dict(original.to_dict()) == original
    assert CrossSection.from_dict(None).sample_size == 0


# --- 総合 ---------------------------------------------------------------------


def test_uncertainty_lowers_the_median_not_raises_it(config):
    """27.14で修正した最重要の論理破綻のリグレッションテスト。

    点推定を分布の**平均**として扱い μ = ln(E) − σ²/2 とすることで、
    ばらつきが中心的な見通しを膨らませる経路を断っている。この補正が無いと、
    中央値では3割の損失が見込まれる銘柄(DEC)が σ=2.50 という大きさだけを
    理由に P(10x)=9.5% で8位に入っていた。
    """
    stable = compute_moic(make_inputs(revenue_growth_volatility=0.10), NEUTRAL, config)
    volatile = compute_moic(make_inputs(revenue_growth_volatility=0.60), NEUTRAL, config)
    assert stable is not None and volatile is not None
    # 期待倍率(点推定)は同じ。違うのはばらつきだけ。
    assert stable.expected_moic == pytest.approx(volatile.expected_moic)
    assert volatile.log_moic_sigma > stable.log_moic_sigma
    # 平均を保ったまま分散が広がるので、中央値は必ず下がる。
    assert volatile.median_moic < stable.median_moic


def test_value_destroying_outlook_is_never_ranked(config):
    """27.17:中心的な見通しが株主価値を毀損する銘柄は10倍候補ではない。

    対数正規は期待値を固定したまま分散を広げると閾値超過確率を上げる。これ自体は
    数学的にも経済的にも正しい(同じ期待値ならボラティリティが高いほうが10倍に
    届きやすい)が、**期待値が1を下回る銘柄**に適用すると「モデルが外れることに
    賭ける」順位づけになる。実データでは上位50銘柄のうち5銘柄が該当していた。
    """
    shrinking = make_inputs(
        revenue_cagr=-0.20, revenue_yoy=-0.25, revenue_growth_volatility=0.60, gross_margin_prior=0.60
    )
    assert compute_moic(shrinking, NEUTRAL, config) is None


def test_expected_moic_of_ranked_names_is_at_least_one(config):
    """ランキングに載る銘柄は必ず「中心的な見通しでプラス」であること。"""
    for growth in (0.05, 0.25, 0.60):
        result = compute_moic(make_inputs(revenue_cagr=growth, revenue_yoy=growth), NEUTRAL, config)
        if result is not None:
            assert result.expected_moic >= config.requirements.min_expected_moic


def test_dilution_materially_reduces_the_probability(config):
    """15.1④「希薄化は単独で最大の改善余地」が実際に効いていること。"""
    fast = dict(revenue_cagr=0.55, revenue_yoy=0.55)
    clean = compute_moic(make_inputs(dilution_cagr=0.0, **fast), NEUTRAL, config)
    dilutive = compute_moic(make_inputs(dilution_cagr=0.15, **fast), NEUTRAL, config)
    assert clean is not None and dilutive is not None
    assert dilutive.probability < clean.probability / 2
    assert dilutive.dilution_drag > 2.5  # 1.15^7


def test_heavy_dilution_alone_can_disqualify_a_grower(config):
    """成長していても、希薄化が成長を食い切れば候補ではなくなる。

    15.1④が「④は常にマイナス寄与(0.6〜0.9x)」としているとおり、この軸だけで
    ①の成果が丸ごと打ち消されうる。
    """
    result = compute_moic(
        make_inputs(revenue_cagr=0.10, revenue_yoy=0.10, dilution_cagr=0.30), NEUTRAL, config
    )
    assert result is None


def test_dilution_missing_uses_cross_section_median_not_zero(config):
    """A-1(docs/model_audit_v4_2026-08-26.md): `dilution_cagr` が欠損している銘柄を
    「希薄化ゼロ」(=最良シナリオ)として扱うのは、27.1の「欠損を減点に読み替え
    ない」の裏返しで「欠損を満点に読み替える」ことになっていた(BRUN型)。
    断面の中央値を中立値として使うべきで、ゼロへは決め打ちしない。
    """
    fast = dict(revenue_cagr=0.55, revenue_yoy=0.55)
    cross_section = CrossSection(median_log_momentum=None, median_log_sigma=None, sample_size=500, median_dilution_cagr=0.08)
    missing = compute_moic(make_inputs(dilution_cagr=None, **fast), cross_section, config)
    zero = compute_moic(make_inputs(dilution_cagr=0.0, **fast), cross_section, config)
    median = compute_moic(make_inputs(dilution_cagr=0.08, **fast), cross_section, config)
    assert missing is not None and zero is not None and median is not None
    assert missing.dilution_drag == pytest.approx(median.dilution_drag)
    assert missing.dilution_drag != pytest.approx(zero.dilution_drag)
    assert missing.dilution_data_missing is True
    assert zero.dilution_data_missing is False


def test_dilution_missing_without_cross_section_falls_back_to_zero(config):
    """A-1: 断面統計そのものが無い(1周目のCrossSection構築時等)場合は、
    従来どおり0.0(中立寄りの安全側フォールバック)にする。"""
    result = compute_moic(make_inputs(dilution_cagr=None, revenue_cagr=0.55, revenue_yoy=0.55), NEUTRAL, config)
    assert result is not None
    assert result.dilution_drag == pytest.approx(1.0)


def test_net_debt_data_missing_flag_flows_through_to_result(config):
    """E-1(docs/defect_audit_2026-08-27.md): `MoicInputs.net_debt_data_missing` が
    `MoicResult` までそのまま伝播すること(計算式は変えない診断フラグ)。"""
    flagged = compute_moic(make_inputs(net_debt_data_missing=True), NEUTRAL, config)
    clean = compute_moic(make_inputs(net_debt_data_missing=False), NEUTRAL, config)
    assert flagged is not None and clean is not None
    assert flagged.net_debt_data_missing is True
    assert clean.net_debt_data_missing is False
    # 診断フラグは確率・期待倍率に影響しない。
    assert flagged.probability == pytest.approx(clean.probability)


def test_growth_raises_the_probability(config):
    """マルチプル圧縮を入れてもなお、成長は確率を押し上げること(28.2)。

    圧縮は「今の価格が既に払っている成長の対価」を差し引くだけであり、成長を
    無意味にするものではない。ここが逆転していたら κ が大きすぎる。
    """
    slow = compute_moic(make_inputs(revenue_cagr=0.03, revenue_yoy=0.03), NEUTRAL, config)
    fast = compute_moic(make_inputs(revenue_cagr=0.50, revenue_yoy=0.50), NEUTRAL, config)
    assert slow is not None and fast is not None
    assert fast.probability > slow.probability


def test_the_model_is_scale_invariant_by_default(config):
    """**v4は規模に対して中立である。これは既定の設計判断であり、バグではない。**

    恒等式 `MOIC = 売上倍率 × 利益率倍率 × マルチプル倍率 × レバレッジ ÷ 希薄化`
    は本質的にスケール不変である。ネットデットが0なら現在のマルチプルは
    完全に約分され、時価総額は確率に一切影響しない。

    v3は2つの経路で規模に傾きを付けていたが、v4はどちらも外した:
    - セクター中央値への平均回帰(28.2:順位を悪化させていた)
    - 規模の事前分布(28.7:1年・2年どちらのホライズンでも支持されなかった)

    旧v2の `corr(スコア, ln時価総額) = +0.227`(大型ほど高得点)という
    **逆向きの傾き**が問題だったのであって、傾きが無いこと自体は問題ではない。
    ユニバース自体が時価総額$3.5B以下に絞られているため、この中で更に規模へ
    傾けるだけの根拠は実データに無い。
    """
    small = compute_moic(make_inputs(market_cap=1.0e8, net_debt=0.0), NEUTRAL, config)
    big = compute_moic(make_inputs(market_cap=3.0e9, net_debt=0.0), NEUTRAL, config)
    assert small is not None and big is not None
    assert big.probability == pytest.approx(small.probability)


def test_size_prior_restores_a_small_cap_tilt_when_enabled(config):
    """28.7:規模の傾きが必要になったら `size_prior.exponent` で復活させられる。"""
    tilted = config.model_copy(
        update={"size_prior": config.size_prior.model_copy(update={"exponent": 0.15})}
    )
    small = compute_moic(make_inputs(market_cap=1.0e8, net_debt=0.0), NEUTRAL, tilted)
    big = compute_moic(make_inputs(market_cap=3.0e9, net_debt=0.0), NEUTRAL, tilted)
    assert small is not None and big is not None
    assert big.probability < small.probability


def test_expected_moic_is_the_product_of_the_five_factors(config):
    """UIが内訳として見せる分解が、実際に恒等式として閉じていること。"""
    result = compute_moic(make_inputs(dilution_cagr=0.05, net_debt=1.0e8), NEUTRAL, config)
    assert result is not None
    reconstructed = (
        result.revenue_multiple
        * result.margin_multiple
        * result.multiple_change
        * result.leverage_effect
        / result.dilution_drag
    )
    assert reconstructed == pytest.approx(result.expected_moic, rel=1e-9)


def test_median_is_below_expected_value(config):
    """対数正規なので中央値 < 平均。UIが両方出すため関係が崩れていないか見る。"""
    result = compute_moic(make_inputs(), NEUTRAL, config)
    assert result is not None
    assert result.median_moic < result.expected_moic


def test_nowcast_is_reported_for_transparency(config):
    """補正**前**の成長率と補正量を必ず持ち帰ること(28.3)。

    株価が成長率推定をどれだけ動かしたかが見えないと、利用者は「なぜこの銘柄が
    上位なのか」を検証できない。恒等式モデルの説明可能性を守るための要件。
    """
    cross_section = CrossSection(median_log_momentum=0.0, median_log_sigma=None, sample_size=500)
    result = compute_moic(make_inputs(log_momentum_12m=0.80), cross_section, config)
    assert result is not None
    assert result.growth_nowcast_adjustment > 0
    assert result.initial_growth_rate == pytest.approx(
        result.base_growth_rate + result.growth_nowcast_adjustment
    )


# --- 「測れない」を「低スコア」に読み替えないこと -----------------------------


def test_extreme_leverage_is_unmeasurable_not_low_scoring(config):
    """27.13:株式がEVのごく一部しか占めない銘柄は、実質的に負債へのコール
    オプションであり対数正規のMOICモデルが成立しない。低スコアではなくNone。

    CRMT(時価総額$21M・ネットデット$725M=EVの2.8%)は、この判定が無かった
    ときに P(10x)=75% で総合1位になっていた。
    """
    assert compute_moic(make_inputs(market_cap=2.1e7, net_debt=7.25e8), NEUTRAL, config) is None


def test_missing_growth_is_unmeasurable(config):
    assert compute_moic(make_inputs(revenue_cagr=None, revenue_yoy=None), NEUTRAL, config) is None


def test_no_gross_profit_is_unmeasurable(config):
    """粗利が無い企業にはユニットエコノミクスが無く、モデルの分母が定義できない。"""
    assert compute_moic(make_inputs(gross_profit_latest=0.0), NEUTRAL, config) is None


def test_net_cash_exceeding_market_cap_is_unmeasurable(config):
    """EVがマイナスになるとEV倍率が定義できない。"""
    assert compute_moic(make_inputs(market_cap=1.0e8, net_debt=-2.0e8), NEUTRAL, config) is None


def test_probability_is_a_valid_probability(config):
    """順位が付く銘柄の確率は必ず [0, 1]。付かない銘柄は None(=測れない)。"""
    ranked = 0
    for growth in (-0.30, 0.0, 0.25, 0.60):
        result = compute_moic(make_inputs(revenue_cagr=growth, revenue_yoy=growth), NEUTRAL, config)
        if result is None:
            continue
        ranked += 1
        assert 0.0 <= result.probability <= 1.0
    assert ranked >= 2


# --- σ の縮小中心とホライズン(27.24、2026-08-26に発見した欠陥) --------------


def _sample_universe():
    return [
        make_inputs(
            revenue_cagr=0.05 + 0.05 * i,
            revenue_yoy=0.04 + 0.05 * i,
            gross_margin_latest=0.20 + 0.02 * i,
            gross_profit_latest=2.0e8 * (0.20 + 0.02 * i),
            market_cap=1.0e8 * (1 + i),
        )
        for i in range(12)
    ]


def test_cross_section_records_the_horizon_it_was_measured_at(config):
    cross_section = build_cross_section(_sample_universe(), config)
    assert cross_section.horizon_years == config.horizon_years
    assert CrossSection.from_dict(cross_section.to_dict()).horizon_years == config.horizon_years


def test_sigma_center_follows_the_requested_horizon(config):
    """27.24の「何年で何倍」の読み替えで σ の中心が7年のまま据え置かれていた欠陥。

    σ はホライズンとともに伸びる。中心だけ7年のままだと、3年へ読み替えたとき
    σ が**引き上げられ**、対数正規の閾値超過確率が過大に出る。実測(この合成
    ユニバース)では P(3年で3倍)が 4.76% → 5.70% と2割過大だった。

    正解は「その年数で断面ごと組み直した値」。それに十分近いことを確認する。
    """
    universe = _sample_universe()
    stored = build_cross_section(universe, config)  # 保存される7年の断面
    short = config.model_copy(update={"horizon_years": 3, "target_moic": 3.0})
    rebuilt = build_cross_section(universe, short)  # 厳密解(断面ごと組み直し)

    subject = make_inputs()
    approximated = compute_moic(subject, stored, short)
    exact = compute_moic(subject, rebuilt, short)
    assert approximated is not None and exact is not None
    assert approximated.log_moic_sigma == pytest.approx(exact.log_moic_sigma, rel=0.05)

    # 既定のホライズンでは何も起きない(倍率1.0で素通り)
    default = compute_moic(subject, stored, config)
    assert default is not None
    assert default.log_moic_sigma == pytest.approx(
        compute_moic(subject, build_cross_section(universe, config), config).log_moic_sigma
    )


def test_legacy_cross_section_without_horizon_is_left_alone(config):
    """`horizon_years` を持たない古い保存行(0=不明)は引き直さない。
    分からないものを推測して動かすより、従来どおりの値を返すほうが安全。"""
    legacy = CrossSection(median_log_momentum=0.0, median_log_sigma=0.76, sample_size=500)
    assert legacy.horizon_years == 0
    short = config.model_copy(update={"horizon_years": 3, "target_moic": 3.0})
    result = compute_moic(make_inputs(), legacy, short)
    assert result is not None


# --- J-4(docs/investment_decision_gap_2026-08-29.md):実現倍率の分位点 ---


def test_moic_quantiles_match_plain_lognormal_when_survival_is_one():
    mu, sigma = 0.8, 0.9
    q = moic_quantiles(mu, sigma, 1.0, (0.1, 0.5, 0.9))
    assert q[0.5] == pytest.approx(math.exp(mu))
    # P90: exp(mu + sigma * z_0.9)
    from statistics import NormalDist

    assert q[0.9] == pytest.approx(math.exp(mu + sigma * NormalDist().inv_cdf(0.9)))
    assert q[0.1] == pytest.approx(math.exp(mu + sigma * NormalDist().inv_cdf(0.1)))


def test_moic_quantiles_put_low_percentiles_at_zero_when_survival_below_that_mass():
    q = moic_quantiles(0.8, 0.9, 0.85, (0.10, 0.25, 0.50, 0.90))
    # 1 - S = 0.15 → q <= 0.15 は 0.0
    assert q[0.10] == 0.0
    assert q[0.25] > 0.0
    assert q[0.50] > 0.0


def test_moic_quantiles_are_monotonically_increasing():
    q = moic_quantiles(0.5, 1.1, 0.7, (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95))
    values = [q[k] for k in (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)]
    assert values == sorted(values)


def test_moic_quantile_median_is_consistent_with_survival_adjustment():
    # 生存確率 0.5 なら、混合分布の中央値(q=0.5)は条件付き分布の q=0 相当、
    # つまり 1-S = 0.5 の境界。conditional_q = 0 → z = -inf は避けられ 0.0 になる。
    q = moic_quantiles(1.0, 0.8, 0.5, (0.5,))
    assert q[0.5] == 0.0
    # 生存確率 0.75 なら中央値は条件付き q = (0.5-0.25)/0.75 = 1/3
    from statistics import NormalDist

    q2 = moic_quantiles(1.0, 0.8, 0.75, (0.5,))
    assert q2[0.5] == pytest.approx(math.exp(1.0 + 0.8 * NormalDist().inv_cdf(1 / 3)))
