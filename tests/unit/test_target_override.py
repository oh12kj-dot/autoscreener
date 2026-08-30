"""任意の「何年で何倍」で再計算できることのテスト(27.24)。"""

import pytest

from autoscreener.config import load_scoring_config
from autoscreener.scoring.moic import MoicInputs, compute_moic

from test_moic import NEUTRAL, make_inputs  # 共通のヘルパーを再利用(tests/unit は rootdir 直下)


@pytest.fixture(scope="module")
def config():
    return load_scoring_config()


def with_target(config, horizon: int, target: float):
    return config.model_copy(update={"horizon_years": horizon, "target_moic": target})


# --- 入力の永続化(再計算の前提) ---------------------------------------------


def test_inputs_round_trip_through_json():
    """`scores.inputs` に保存して読み戻しても同じ入力になること。

    これが壊れると、目標を変えたときのランキングが静かに別物になる。
    `float("inf")`(黒字企業のキャッシュランウェイ)はJSONで表せないため
    エスケープしている——そこが一番壊れやすい。
    """
    import json
    import math

    original = make_inputs(cash_runway_quarters=math.inf)
    restored = MoicInputs.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original


# --- 目標を変えたときの挙動 ---------------------------------------------------


def test_longer_horizon_raises_the_expected_multiple(config):
    """成長企業なら、年数を伸ばすほど複利が効いて期待倍率は上がる。"""
    short = compute_moic(make_inputs(), NEUTRAL, with_target(config, 3, 10.0))
    long = compute_moic(make_inputs(), NEUTRAL, with_target(config, 10, 10.0))
    assert short is not None and long is not None
    assert long.expected_moic > short.expected_moic


def test_lower_target_raises_the_probability(config):
    """同じ年数なら、目標倍率が低いほど達成確率は高い。"""
    ten = compute_moic(make_inputs(), NEUTRAL, with_target(config, 7, 10.0))
    five = compute_moic(make_inputs(), NEUTRAL, with_target(config, 7, 5.0))
    assert ten is not None and five is not None
    assert five.probability > ten.probability


def test_three_years_to_triple_is_harder_than_seven_years_to_ten(config):
    """**年数と倍率を別々に見ると難易度を取り違える。**

    3年で3倍は年率44.2%、7年で10倍は年率38.9%。前者のほうが厳しい。
    UIが必要年率を必ず併記するのはこの関係を見せるため。
    """
    assert 3 ** (1 / 3) - 1 > 10 ** (1 / 7) - 1


def test_dilution_outrunning_growth_disqualifies_longer_horizons(config):
    """**希薄化と成長のどちらが速いかで、ホライズンの向きが決まる。**

    v3ではここに「短いホライズンほどマルチプルの下方回帰を成長で取り返せない」
    というテストがあったが、v4はマルチプルの平均回帰そのものを撤廃したため
    (28.2)、その機序は存在しなくなった。

    v4で残る非対称は複利の綱引きである。年率10%の希薄化は12年で3.14倍になり、
    年率30%の成長は減衰するため12年目には4%まで落ちる。**希薄化は減衰せず、
    成長は減衰する**——だから年数を伸ばすほど希薄化が勝ちやすくなる。
    27.24が「年数を変えると対象銘柄が入れ替わる」と書いているのはこの効果。
    """
    diluting = make_inputs(revenue_cagr=0.30, revenue_yoy=0.30, dilution_cagr=0.10)
    short = compute_moic(diluting, NEUTRAL, with_target(config, 3, 10.0))
    long = compute_moic(diluting, NEUTRAL, with_target(config, 12, 10.0))
    assert short is not None  # 3年なら成長がまだ希薄化に勝っている
    assert long is None  # 12年では希薄化が成長を食い切り、順位を付けない


def test_survival_probability_shrinks_with_the_horizon(config):
    """生存確率は年数の複利なので、長い目標ほど下がる。"""
    short = compute_moic(make_inputs(), NEUTRAL, with_target(config, 3, 10.0))
    long = compute_moic(make_inputs(), NEUTRAL, with_target(config, 10, 10.0))
    assert short is not None and long is not None
    assert short.survival_probability > long.survival_probability


def test_default_target_matches_the_config(config):
    """既定で呼んだときは設定値どおりに計算されること(保存値と一致する前提)。"""
    explicit = compute_moic(
        make_inputs(), NEUTRAL, with_target(config, config.horizon_years, config.target_moic)
    )
    implicit = compute_moic(make_inputs(), NEUTRAL, config)
    assert explicit is not None and implicit is not None
    assert explicit.probability == pytest.approx(implicit.probability)
