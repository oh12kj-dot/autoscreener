"""スコアの内訳を人間が読める形にする(14.16・27.17)。

旧実装は8サブスコアそれぞれについて「この銘柄の実測値を代入した計算式」を
文字列で組み立てていた(375行)。実現倍率モデルではその必要が無い——
スコアは恒等式の**積**なので、各因子が「MOICを何倍にしているか」をそのまま
並べれば内訳が完結する。

    expected_moic = revenue_multiple × margin_multiple × multiple_change
                    × leverage_effect ÷ dilution_drag

この5つはすべて「1.0が中立、1.0より大きければMOICを押し上げている」という
同じ尺度で読める。どの因子がスコアを作っているのかが一目で分かる形は、
0〜100のサブスコアを8本並べるより情報量が多い。
"""

from __future__ import annotations

from autoscreener.api.schemas import FactorBreakdown

# (factorsのキー, 表示名, 逆数として効くか, 説明)
_FACTOR_SPECS: list[tuple[str, str, bool, str]] = [
    (
        "revenue_multiple",
        "① 売上の成長",
        False,
        "初期成長率を毎年減衰させながら7年間複利で積んだ売上倍率(15.1①)。"
        "初期成長率は3年CAGRと直近年次YoYの小さいほうを採り、"
        "決算の陳腐化(最大15ヶ月)を株価の相対トレンドで補正する(28.3)。"
        "減衰の速さは事業の質(Piotroski F-score)で変わる——質が高いほど成長が長続きする(28.10)。",
    ),
    (
        "margin_multiple",
        "② 利益率の変化",
        False,
        "直近2期の粗利率トレンドを減衰させて7年後まで外挿したときの、粗利率の変化倍率(15.1②)。",
    ),
    (
        "multiple_change",
        "③ マルチプルの変化",
        False,
        "**今の株価が既に払っている成長の対価**を差し引く項(28.2)。"
        "市場は成長率が1pt高い企業にEV/粗利を約0.86%高く付けている(断面から実測)。"
        "モデルが7年かけて成長を減速させる以上、その分だけ倍率は下がるのが整合的である。"
        "v3にあった「割安な銘柄はセクター中央値へ上方回帰する」という無償の再評価は撤廃した"
        "——実測で順位を悪化させていたため(順位IC −0.023)。",
    ),
    (
        "leverage_effect",
        "有利子負債の影響",
        False,
        "事業価値(EV)の変化が株主価値に増幅されて伝わる倍率。"
        "負債が多いほど1を超え、ネットキャッシュなら1を下回る。ばらつきにも同じ倍率で効く。",
    ),
    (
        "dilution_drag",
        "④ 希薄化",
        True,
        "発行済株式数の年率CAGRを7年複利にした値(15.1④)。1株あたりの取り分を減らすので割り算で効く。"
        "15.1はこの軸を「単独で最大の改善余地」としている。",
    ),
]


def build_factor_breakdown(factors: dict[str, float] | None) -> list[FactorBreakdown]:
    """`scores.factors` を、寄与倍率つきの内訳リストに変換する。

    `contribution` は「この因子がMOICを何倍にしているか」に統一する。希薄化は
    割り算で効くので逆数を取り、他の因子と同じ向き(1.0より大きい=有利)で
    並べられるようにする。
    """
    if not factors:
        return []

    breakdown: list[FactorBreakdown] = []
    for key, label, is_divisor, explanation in _FACTOR_SPECS:
        value = factors.get(key)
        if value is None:
            continue
        contribution = (1 / value) if (is_divisor and value > 0) else value
        breakdown.append(
            FactorBreakdown(
                key=key, label=label, value=value, contribution=contribution, explanation=explanation
            )
        )
    return breakdown


def diagnostic_labels() -> dict[str, str]:
    """`factors` に入っている診断値の表示名(内訳5因子以外)。"""
    return {
        "expected_moic": "点推定(期待値)の実現倍率",
        "initial_growth_rate": "初期成長率(ナウキャスト補正後・減衰前)",
        "base_growth_rate": "初期成長率(財務諸表のみ)",
        "growth_nowcast_adjustment": "株価トレンドによる成長率の補正量",
        "terminal_growth_rate": "7年目の成長率(減衰後)",
        "growth_fade_rate": "成長の減衰率(質が高いほど大きい)",
        "raw_log_moic_sigma": "σ(断面への縮小推定前)",
        "terminal_gross_margin": "7年後の粗利率",
        "current_ev_to_gross_profit": "現在のEV/粗利",
        "target_ev_to_gross_profit": "7年後のEV/粗利",
        "implied_terminal_ev": "7年後の事業価値(推定)",
        "health_index": "財務健全性指標(−1〜+1)",
        "size_prior": "規模の事前分布による補正",
    }
