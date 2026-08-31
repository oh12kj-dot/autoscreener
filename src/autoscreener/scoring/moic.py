"""実現時価総額倍率(implied MOIC)モデル v4(27章・28章)。

15.1の恒等式

    株価 = 売上 × 利益率 × マルチプル ÷ 発行済株式数

を、パーセンタイルに潰さずそのまま**積**として計算する。旧v2(8サブスコアの
加重幾何平均)を捨てた理由は27.1に、v3からv4への変更理由は28章にある。

**v3からv4で変わった点(28章)**。v3の擬似バックテストはリフト倍率1.21・
デシル単調性+0.59に留まっていた。因子ごとに予測力を実測したところ、原因は
**モデルが持っていた終端マルチプルの「セクター中央値への平均回帰」項が、
順位を積極的に悪化させていた**ことにあった(順位IC −0.023、t=−3.1、9評価日中
7日で負)。v4はこの項を撤廃し、代わりに次の3点を入れた。

1. **成長フェード整合のマルチプル圧縮**(28.2)——「割安だから上がる」ではなく
   「**今の株価は今の成長率を既に織り込んでいる**」を計算に入れる
2. **決算の陳腐化に対する価格ナウキャスト**(28.3)——年次決算は評価時点で最大
   15ヶ月古い。その間の株価の相対的な動きを成長率推定の修正に使う
3. **σ の縮小推定**(28.4)——断面の σ 推定は誤差が大きく、そのばらつきを
   そのまま確率に通すと順位が σ のノイズに支配される

**出力はスコアではなく確率**。7年後の1株あたり実現倍率(MOIC)の対数を正規分布と
仮定し、`P(MOIC >= 10)` を返す。14.2が「小型株が10年で10倍になる基準率は1%未満」
としているとおり、この値は通常0.01%〜数%のオーダーになる。

**すべての入力は年次財務諸表と価格系列のみから作る**。`info` 由来のTTM値・
アナリスト予想・機関保有率などは過去時点に遡って再構成できず、それらを使うと
モデルが永久に検証不能になる(14.3)。本モジュールが年次データと価格だけで
閉じていることにより、`backtest/` が同一のロジックを過去時点で走らせられる。

すべて純粋関数。DBにもyfinanceにも触れない。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from statistics import NormalDist

from autoscreener.config import ScoringConfig

_NORMAL = NormalDist()

# 生存確率のハザード率をこの範囲に丸める(モデルの外挿が極端にならないように)
_MIN_ANNUAL_HAZARD = 0.001
_MAX_ANNUAL_HAZARD = 0.50

# 規模の事前分布が確率へ与える倍率の許容範囲
_MIN_SIZE_PRIOR = 0.20
_MAX_SIZE_PRIOR = 2.00


@dataclass(frozen=True)
class MoicInputs:
    """1銘柄・1時点分のモデル入力。すべて年次財務諸表と価格系列から作れる値。

    `point_in_time.py` が過去・現在どちらの時点でもこれを組み立てる。
    """

    # --- 規模と評価 -----------------------------------------------------------
    market_cap: float
    net_debt: float  # 有利子負債 − 現金。マイナス(ネットキャッシュ)もありうる
    revenue_latest: float
    gross_profit_latest: float

    # --- 成長 -----------------------------------------------------------------
    revenue_cagr: float | None  # 年次売上の複数年CAGR
    revenue_yoy: float | None  # 直近年次 vs 前年次
    revenue_growth_volatility: float | None  # 過去の年次成長率の標準偏差

    # --- 利益率 ---------------------------------------------------------------
    gross_margin_latest: float
    gross_margin_prior: float | None

    # --- 希薄化 ---------------------------------------------------------------
    dilution_cagr: float | None

    # --- 生存力 ---------------------------------------------------------------
    piotroski_ratio: float | None
    cash_runway_quarters: float | None
    equity_to_assets: float | None
    fcf_margin: float | None

    sector: str | None = None

    # --- 循環性(30.1) ---------------------------------------------------------
    # 年次系列の変化が一方向に積み上がっているか(1)、上下に振れているだけか(0)。
    # `point_in_time.series_trend_consistency` が測る。3期未満では None。
    # 点推定の外挿量をこの一致度で割り引くために使う(σ ではなく **バイアス**の
    # 補正である点が重要——詳細は `point_in_time.series_trend_consistency`)。
    gross_margin_consistency: float | None = None
    revenue_trend_consistency: float | None = None

    # --- 価格トレンド(28.3のナウキャスト用) -----------------------------------
    # 直近12ヶ月の**対数**リターン。年次決算が評価時点で最大15ヶ月古いという
    # 構造的な陳腐化を埋めるための、唯一の価格由来入力。`price_snapshots` から
    # 過去時点でもそのまま引けるため、ライブとバックテストの同一性は崩れない。
    log_momentum_12m: float | None = None

    # --- S-5診断用(2026-08-26、docs/model_audit_v4_2026-08-26.md) ------------------
    # `net_debt` に含まれるオペレーティングリース債務の推定額。取得できない
    # 場合はNone(=診断できない。net_debtの計算そのものは変えない)。
    lease_liability: float | None = None

    # --- E-1診断用(2026-08-27、docs/defect_audit_2026-08-27.md) --------------------
    # `net_debt` の構成要素(Total Debt / 現金)のいずれかが貸借対照表に無く、
    # `or 0.0` で補完された場合に True。計算そのものは変えず、A-1と同型の
    # 「欠損を有利な値へ読み替える」挙動を利用者に可視化するためのフラグ。
    net_debt_data_missing: bool = False

    def to_dict(self) -> dict[str, float | str | None]:
        """`scores.inputs` に保存できる形にする(27.24)。

        保存しておくと、**任意のホライズン・目標倍率で厳密に再計算できる**。
        対数正規を時間で引き伸ばす近似(`μ_f = μ·f, σ_f = σ·√f`)ではなく、
        成長の減衰・希薄化の複利・生存確率をその年数で計算し直せるので、
        「3年で3倍」のような短いホライズンでも歪まない。
        `float("inf")` はJSONで表せないため文字列にエスケープする。
        """
        payload: dict[str, float | str | None] = {}
        for field_name, value in self.__dict__.items():
            if isinstance(value, float) and math.isinf(value):
                payload[field_name] = "inf" if value > 0 else "-inf"
            else:
                payload[field_name] = value
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> MoicInputs:
        restored = dict(payload)
        for key, value in restored.items():
            if value == "inf":
                restored[key] = math.inf
            elif value == "-inf":
                restored[key] = -math.inf
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in restored.items() if k in known})


@dataclass(frozen=True)
class CrossSection:
    """同一評価日のユニバース全体から作る、銘柄間で共有される統計(28.5)。

    v3はここに「セクター別のEV/GrossProfit中央値」を置き、終端マルチプルの
    回帰先にしていた。v4はその平均回帰そのものを撤廃したため(28.2)、
    クロスセクションが担う役割は次の2つに変わった。

    - `median_log_momentum`:ナウキャストの基準線。個別銘柄の12ヶ月リターンを
      **市場全体の動き**から切り離すために引く。これが無いと、強気相場では
      全銘柄の成長率が一律に上方修正されてしまい、順位に何の情報も加わらない
    - `median_log_sigma`:σ の縮小推定(28.4)の中心

    `scores.inputs` に一緒に保存され、API が任意のホライズンで再計算するときに
    復元される(27.24)。
    """

    median_log_momentum: float | None = None
    median_log_sigma: float | None = None
    sample_size: int = 0
    # 30.3:終端 EV/粗利の絶対上限。当日の断面の分位点から作る(無効なら None)。
    ev_to_gross_profit_cap: float | None = None
    # 30.5:断面の値づけ線 ln(EV/粗利) = c + κ·g の切片 c。
    # 残差 ε = ln M_0 − (c + κ·g_0) が「同じ成長率の銘柄と比べた割高/割安」。
    log_multiple_intercept: float | None = None
    # A-1(2026-08-26、docs/model_audit_v4_2026-08-26.md):希薄化データが欠損している
    # 銘柄に使う中立値。断面の中央値を使う。理由は `compute_moic` 側のコメント参照。
    median_dilution_cagr: float | None = None
    # `median_log_sigma` を測ったときのホライズン(年)。σ はホライズンとともに
    # 伸びるので、**この値を保存しておかないと「3年で3倍」への読み替え(27.24)で
    # 7年の σ 中心をそのまま当ててしまう**。0 は「不明」(v4初期に保存された行)。
    horizon_years: int = 0

    def to_dict(self) -> dict:
        return {
            "median_log_momentum": self.median_log_momentum,
            "median_log_sigma": self.median_log_sigma,
            "sample_size": self.sample_size,
            "median_dilution_cagr": self.median_dilution_cagr,
            "horizon_years": self.horizon_years,
            "ev_to_gross_profit_cap": self.ev_to_gross_profit_cap,
            "log_multiple_intercept": self.log_multiple_intercept,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> CrossSection:
        if not payload:
            return cls()
        return cls(
            median_log_momentum=payload.get("median_log_momentum"),
            median_log_sigma=payload.get("median_log_sigma"),
            sample_size=payload.get("sample_size", 0),
            median_dilution_cagr=payload.get("median_dilution_cagr"),
            horizon_years=payload.get("horizon_years", 0),
            ev_to_gross_profit_cap=payload.get("ev_to_gross_profit_cap"),
            log_multiple_intercept=payload.get("log_multiple_intercept"),
        )


@dataclass(frozen=True)
class MoicResult:
    """モデル出力。`probability` がランキングのキー、残りはすべて説明用。

    中央値MOICは次の恒等式で分解できる(UIの内訳表示はこの5因子を使う):

        expected_moic = revenue_multiple × margin_multiple × multiple_change
                        × leverage_effect ÷ dilution_drag
    """

    probability: float  # P(MOIC >= target_moic)。生存確率・規模の事前分布込み
    expected_moic: float  # 各因子の中心的見通しを掛け合わせた点推定(=分布の平均)
    median_moic: float  # 上を対数正規の平均とみなしたときの中央値 exp(mu)
    log_moic_mu: float
    log_moic_sigma: float

    survival_probability: float
    size_prior: float

    # 中央値MOICの5因子分解(15.1の恒等式に対応)
    revenue_multiple: float
    margin_multiple: float
    multiple_change: float
    leverage_effect: float
    dilution_drag: float

    # 診断用
    initial_growth_rate: float  # ナウキャスト補正**後**の g0(実際に外挿した値)
    base_growth_rate: float  # 循環性割引**後**・ナウキャスト**前**の g0
    growth_nowcast_adjustment: float  # 上2つの差。価格が成長推定をどれだけ動かしたか
    terminal_growth_rate: float  # 減衰後、ホライズン最終年の成長率
    growth_fade_rate: float  # その銘柄に適用した減衰率(28.10)
    terminal_gross_margin: float
    current_ev_to_gross_profit: float
    target_ev_to_gross_profit: float
    implied_terminal_ev: float
    health_index: float
    raw_log_moic_sigma: float  # 縮小推定**前**の σ

    # --- 診断フラグ(2026-08-26追加、docs/model_audit_v4_2026-08-26.md) ------------
    growth_rate_clamped: bool = False  # S-6:初期成長率が上限/下限に張り付いたか
    dilution_data_missing: bool = False  # A-1:希薄化が欠損し断面中央値で補完したか
    lease_share_of_net_debt: float | None = None  # S-5:ネットデットに占めるリース債務の割合
    net_debt_data_missing: bool = False  # E-1:net_debtの構成要素が欠損し0.0で補完したか
    # D-6:ホライズン終端の射影ネットデットと、評価時点からの変化。
    # `balance_sheet.project_net_debt` が False でも**計算して診断値として出す**
    # (S-5/E-1 と同じ「まず可視化」の手順)。
    projected_net_debt: float | None = None
    net_debt_change: float | None = None

    # --- 30章(循環性・終端バリュエーション)の診断値 --------------------------
    # 財務諸表だけから出した g0(循環性割引もナウキャストも掛ける前)。
    statement_growth_rate: float = 0.0
    # 循環性割引が g0 を動かした量(`base_growth_rate − statement_growth_rate`)。
    growth_cyclicality_adjustment: float = 0.0
    # 終端 EV/粗利が断面由来の上限に当たったか(30.3)。
    terminal_multiple_capped: bool = False
    # 循環性の一致度(そのまま入力を写す。UIの警告バッジ用)。
    revenue_trend_consistency: float | None = None
    gross_margin_consistency: float | None = None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def base_initial_growth(inputs: MoicInputs, config: ScoringConfig) -> float | None:
    """財務諸表だけから出す初期成長率 g0。**3年CAGRと直近年次YoYの小さいほう**。

    片方しか無ければそれを使う。両方無ければモデルは成立しない(None)。

    **なぜ加重平均ではなく最小値なのか(27.13)**:当初は「長い窓を重く見る」
    加重平均にしていたが、実データで2種類の failure mode を同時に踏んだ。

    - **買収による見かけの成長**(15.3):IMMRは連結範囲の変更で3年CAGRが
      +214%になっていた。直近年次YoYは+11%で既に正常化していたのに、
      加重平均(0.6:0.4)では+133%となり上限に張り付いていた
    - **基数効果**:前年がほぼゼロの期を含む窓では、どちらか一方の指標だけが
      極端な値になる

    どちらの場合も「2つの測定値が大きく食い違っている = 少なくとも一方は
    成長の実力を表していない」という共通構造を持つ。7年間の外挿という用途では、
    食い違ったときに**遅いほうを信じる**のが正しい姿勢である。

    27.21②はこの選択の代償として「加速中の企業を必ず遅いほうに丸めてしまう」
    ことを挙げていた(ランキング対象の15%が該当)。v4の価格ナウキャスト
    (`nowcast_initial_growth`)は、まさにその取りこぼしを埋めるために入っている。

    **S-7(2026-08-26、docs/model_audit_v4_2026-08-26.md)**:上の「食い違ったら
    遅いほうを信じる」安全装置は、CAGR・YoY のどちらか一方しか無い銘柄では
    働かない(比較対象が無いため)。そうした銘柄には、より保守的な上限
    (`max_initial_rate_single_observation`)を適用する。
    """
    growth = config.growth
    candidates = [g for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None]
    if not candidates:
        return None
    return _clamp(min(candidates), growth.min_initial_rate, initial_growth_ceiling(inputs, config))


def raw_initial_growth(inputs: MoicInputs) -> float | None:
    """クランプ**前**の初期成長率 g0(3年CAGRと直近年次YoYの小さいほう)。

    `base_initial_growth` はこの値を `_clamp` で丸めて返す。E-4
    (docs/defect_audit_2026-08-27.md)のマルチプル成長弾力性 κ の推定で、
    「モデル自身の丸めを経由していない生の成長率」を説明変数に使えるように
    切り出したもの。ランキング計算は引き続き `base_initial_growth`(丸め後)を
    使う——丸め自体は外挿の暴走防止という別目的で必要。
    """
    candidates = [g for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None]
    if not candidates:
        return None
    return min(candidates)


def damp_growth_for_cyclicality(
    base_growth: float, inputs: MoicInputs, config: ScoringConfig
) -> float:
    """初期成長率の**循環性割引**(30.1)。超過成長分を年次売上系列の一方向性で割り引く。

        g_eff = terminal + (g0 − terminal) × (1 − damping × (1 − consistency))

    **これは σ ではなく点推定の補正である。** v4 は循環性を
    `revenue_growth_volatility` → σ の経路だけで扱っていたが、σ は 85% 縮小
    されるため断面差はほぼ残らない(28.4)。しかも循環は「ばらつきが大きい」の
    ではなく「**観測時点が循環のどこにあるかで推定量の符号ごと変わる**」という
    バイアスの問題であり、分散に押し込んでも中心的見通しは補正されない。

    実データ(2026-08-28、ランキング716銘柄)では、上位50の40%が
    Basic Materials と Energy で占められていた。両セクターはランキング対象全体の
    11.6% しかなく、上位進出率はそれぞれ 26.0% / 21.2%(Technology 2.3%、
    Healthcare 5.0%)。市況上昇局面の資源会社が「売上も粗利率も急改善している
    企業」として上位に来ていた。価格受容者であり再投資による複利が構造的に
    効かない事業を10バガー候補の上位に置くのは、モデルの推定量が循環と構造を
    区別できていないためである。

    一致度が測れない銘柄(年次3期未満)は補正しない——欠損を減点に読み替えない
    という方針(27.1)をここでも守る。
    """
    growth = config.growth
    if growth.cyclicality_damping <= 0 or inputs.revenue_trend_consistency is None:
        return base_growth
    retained = 1.0 - growth.cyclicality_damping * (1.0 - _clamp(inputs.revenue_trend_consistency, 0.0, 1.0))
    terminal = growth.terminal_rate
    return terminal + (base_growth - terminal) * retained


def initial_growth_ceiling(inputs: MoicInputs, config: ScoringConfig) -> float:
    """その銘柄に適用される初期成長率の上限(S-7)。

    観測が2つ(CAGRとYoY)揃っている銘柄には通常の `max_initial_rate` を、
    片方しか無い銘柄には `max_initial_rate_single_observation` を使う。

    **公開関数にした理由(2026-08-26)**:この上限は `base_initial_growth` の
    中だけで使われており、**価格ナウキャスト(`nowcast_initial_growth`)は
    素の `max_initial_rate` で丸めていた**。つまり観測が1つしかない銘柄でも、
    ナウキャストの補正を経由すれば通常の上限まで戻れてしまい、S-7の安全装置は
    片側からすり抜けられる状態だった。両方が同じ上限を参照するようにする。
    """
    growth = config.growth
    observation_count = sum(
        1 for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None
    )
    return _initial_growth_ceiling(observation_count, growth)


def _initial_growth_ceiling(observation_count: int, growth: "GrowthConfig") -> float:
    """S-7: 成長率の観測数に応じた初期成長率の上限。観測が1つしかない銘柄は
    27.13の「食い違ったら遅いほうを信じる」検証が働かないため、より保守的な
    上限を適用する。"""
    if observation_count >= 2:
        return growth.max_initial_rate
    return min(growth.max_initial_rate, growth.max_initial_rate_single_observation)


def is_initial_growth_clamped(inputs: MoicInputs, config: ScoringConfig) -> bool:
    """S-6: 初期成長率が上限(または下限)に張り付いているか。

    上限に到達した銘柄は「その企業の成長力を測れた」のではなく「モデルの
    外挿範囲を超えていたので丸められた」状態であり、実データでは上位30の
    17%がここに該当した(2026-08-25時点)。ランキングの信頼度をUIに出すための
    診断フラグ。
    """
    growth = config.growth
    candidates = [g for g in (inputs.revenue_cagr, inputs.revenue_yoy) if g is not None]
    if not candidates:
        return False
    raw = min(candidates)
    return raw >= initial_growth_ceiling(inputs, config) or raw <= growth.min_initial_rate


def nowcast_initial_growth(
    base_growth: float, inputs: MoicInputs, cross_section: CrossSection, config: ScoringConfig
) -> tuple[float, float]:
    """価格の相対トレンドで初期成長率を補正する(28.3)。戻り値は (補正後g0, 補正量)。

    **なぜ必要か——モデルは構造的に古い数字を見ている**。評価時点で使える年次
    決算は、期末から開示ラグ90日(`REPORTING_LAG_DAYS`)を置いてはじめて可視に
    なる。つまり最悪の場合、モデルは**15ヶ月前の事業実態**から7年後を外挿して
    いる。その15ヶ月のあいだに市場は決算・ガイダンス・受注動向を織り込んで
    株価を動かしており、その情報をモデルは一切見ていなかった。

    **どう使うか——リターン予測ではなく成長率推定の修正として**。マルチプルの
    成長弾力性 κ(`multiple.growth_elasticity`)は「成長率が Δg 高い企業は
    EV/粗利が exp(κ·Δg) 倍で評価される」という断面の値づけ構造である(28.2)。
    これを逆に読むと、ある銘柄の株価が**市場全体に対して** Δln(P) だけ動いた
    とき、その一部が成長期待の改定によるものだと解釈すれば、含意される成長率の
    改定は Δln(P) / κ になる。

    実際には株価の相対的な動きは成長期待の改定だけでは決まらない(リスク
    プレミアム・流動性・センチメント)。したがって全部は信じず、`nowcast_weight`
    (既定0.25)だけ信じ、さらに `nowcast_cap`(既定±15pt)で丸める。

    **市場全体からの超過分を使う**理由:絶対リターンを使うと、強気相場では
    全銘柄が一律に上方修正され、順位に何の情報も加わらないまま確率の水準だけが
    上がる。中央値を引くことで「市場が織り込んだ以上に評価が変わった銘柄」だけ
    が動く。

    **これはモメンタム戦略ではない**。12ヶ月モメンタムを順位に足しているのでは
    なく、成長率という**モデルの入力**を修正している。したがってナウキャストが
    効いた銘柄でも、粗利率・希薄化・レバレッジが悪ければ順位は上がらない。

    **S-8(2026-08-26、docs/model_audit_v4_2026-08-26.md)**:実測では上位銘柄の
    約3割が `nowcast_cap` の上限に張り付いており、うち複数件は決算ベースの
    成長率が負(縮小)なのに補正で正(成長)へ反転していた。決算という一次
    情報を株価で上書きする補正は、通常の補正より強い証拠を要求すべきなので、
    `base_growth < 0` かつ補正が上方向(反転方向)のときだけ、より狭い
    `nowcast_cap_sign_flip` を上限に使う。
    """
    growth = config.growth
    if growth.nowcast_weight <= 0 or inputs.log_momentum_12m is None:
        return base_growth, 0.0
    if cross_section.median_log_momentum is None:
        return base_growth, 0.0

    elasticity = max(config.multiple.growth_elasticity, 0.25)
    excess = inputs.log_momentum_12m - cross_section.median_log_momentum
    raw_adjustment = growth.nowcast_weight * excess / elasticity
    cap = growth.nowcast_cap
    if base_growth < 0 and raw_adjustment > 0:
        cap = min(cap, growth.nowcast_cap_sign_flip)
    adjustment = _clamp(raw_adjustment, -cap, cap)
    # S-7と整合させる(2026-08-26修正):観測が1つしかない銘柄の上限を
    # `base_initial_growth` だけに掛けても、ここで素の `max_initial_rate` まで
    # 戻せてしまうと安全装置として機能しない。
    adjusted = _clamp(
        base_growth + adjustment, growth.min_initial_rate, initial_growth_ceiling(inputs, config)
    )
    return adjusted, adjusted - base_growth


def growth_fade(inputs: MoicInputs, config: ScoringConfig) -> float:
    """その銘柄の成長減衰率。**事業の質が高いほど成長は長続きする**(28.10)。

    v3は `fade` を全銘柄共通の定数(0.75)にしていた。だがそれは「どんな企業でも
    超過成長は同じ速さで消える」という主張であり、成長株投資の中核にある
    **持続性の差**を最初から無いことにしている。

    持続性の代理指標として Piotroski F-score を使う。F-scoreの9項目は
    収益性(ROA・営業CF・その改善・アクルーアルの少なさ)、レバレッジと流動性、
    営業効率(粗利率と資産回転率の改善)からなり、**直近の決算が改善基調で
    実現しているのか、一時的な山なのか**を測っている。前者なら外挿の出発点は
    信頼でき、後者なら成長は早く消える。

    実データでの位置づけ(28.10):モデル確率の上位デシルの中に限ると、
    Piotroski の順位ICは **+0.138(9評価日中8日で正)** と、モデルが取り
    こぼしている情報として最大だった。生存確率(健全性 → ハザード率)だけでは
    この情報を拾いきれていない(同じ部分集合で生存確率のICは +0.035)。

    減衰率に載せると、上位デシルの破綻率がユニバース比 1.21 → 1.13、
    最悪評価日のリフトが 0.79 → 0.94 に改善する(リフト平均・単調性はほぼ不変)。
    **上振れを削らずに下振れを減らす**方向に働いており、10バガー探索で
    払ってよい種類のコストではない、という判断で採用した。

    F-scoreが算出できない銘柄は中立(基準の fade)に置く。欠損を減点に
    読み替えないという方針(27.1)はここでも守る。
    """
    growth = config.growth
    if growth.fade_quality_sensitivity <= 0 or inputs.piotroski_ratio is None:
        return growth.fade
    quality = _clamp((inputs.piotroski_ratio - 0.5) * 2, -1.0, 1.0)
    return _clamp(
        growth.fade + growth.fade_quality_sensitivity * quality,
        growth.min_fade,
        growth.max_fade,
    )


def growth_path(initial_rate: float, fade: float, config: ScoringConfig) -> list[float]:
    """減衰する成長率の系列 g_1..g_H。

    `g_t = terminal + (g0 - terminal) * fade^t`。高成長がそのまま7年続く前提は
    実証的に成り立たない(成長率は平均回帰する)ため、初期成長率の超過分を毎年
    `fade` 倍に縮める。fade=0.75・H=7なら7年目に初期超過分の13%が残る。

    `fade` は `growth_fade` が銘柄ごとに決める(28.10)。
    """
    terminal = config.growth.terminal_rate
    return [terminal + (initial_rate - terminal) * fade**t for t in range(1, config.horizon_years + 1)]


def terminal_gross_margin(inputs: MoicInputs, config: ScoringConfig) -> float:
    """7年後の粗利率。直近2期のトレンドを減衰させて外挿し、上下限で丸める。

    利益率の改善・悪化はそのまま線形に続かない(競争と平均回帰)ため
    `trend_damping` で縮め、さらに7年間の総変化幅を `max_total_change` に
    制限する。前期の粗利率が取れない場合はトレンド0(現状維持)とする。

    **粗利率の年次系列を使う案(S-3/S-4)は2件とも試して不採用にした**
    (2026-08-26、docs/model_audit_v4_2026-08-26.md §11)。

    - **S-3(トレンドを全期間の最小二乗の傾きにする)**:狙いどおりシクリカル
      銘柄の1年だけの急変動は穏やかになったが、**主指標のデシル単調性が
      0.842 → 0.745 に悪化**した。直近2期の差分は「直近の勢い」を素直に
      反映しており、それ自体に予測力があった。
    - **S-4(EV/粗利の分母を履歴中央値で正規化する)**:順位指標は改善した
      ものの、**確率の水準を壊した**(上位5銘柄の平均が 5.0% → 13.3%)。
      原因は「マルチプルの分母だけ正規化して、終端粗利の予測は正規化して
      いない」不整合による粗利率改善の二重計上。恒等式を展開すると
      実効的な倍率が `terminal_margin / 履歴中央値` になり、赤字から黒字へ
      構造転換した銘柄(ALTO:中央値1.1% → 現在3.8% → 終端11.6%)で
      10倍規模に発散していた。

    そもそも S-4 が狙っていた「粗利が谷なのに倍率を据え置く二重取り」は、
    S-1(フロアの押し上げ修正)で既に解消している——AMRは margin_multiple が
    4.29 → 1.00 になり、期待倍率0.39で `negative_outlook` へ落ちた。
    **したがって粗利率の外挿は直近2期の差分のみを使う。**
    """
    margin = config.margin
    current = inputs.gross_margin_latest
    # S-1の修正漏れ(2026-08-26に発見):`floor` を「外挿の暴走を防ぐ下限」から
    # 「現在値を超えて持ち上げない下限」へ直したとき、**前期の粗利率が取れない
    # 経路だけが直っていなかった**。下の early return が `_clamp(current, floor,
    # ceiling)` のまま残っており、粗利率が floor(5%)を下回る銘柄では終端粗利率が
    # 現在値より上に丸め上げられ、`margin_multiple > 1` という無償の加点になって
    # いた。トレンドがある経路と同じ下限を最初に決めておく。
    lower_bound = min(current, margin.floor)
    if inputs.gross_margin_prior is not None:
        annual_trend = (current - inputs.gross_margin_prior) * margin.trend_damping
        # 30.1:循環性割引。直近2期の差分は「構造的な改善」と「循環の一局面」を
        # 区別しない。年次粗利率系列が一方向に積み上がっているかどうか
        # (`series_trend_consistency`)で外挿量を割り引く。実データでは上位50の
        # 66%が margin_multiple > 1.2 を持ち、その多くが市況で粗利率が反発した
        # 資源・エネルギー銘柄だった。一致度が測れない銘柄は補正しない(27.1)。
        if margin.cyclicality_damping > 0 and inputs.gross_margin_consistency is not None:
            annual_trend *= 1.0 - margin.cyclicality_damping * (
                1.0 - _clamp(inputs.gross_margin_consistency, 0.0, 1.0)
            )
    else:
        # トレンドが測れない = 現状維持。上下限で丸めるだけで、持ち上げはしない。
        return _clamp(current, lower_bound, margin.ceiling)
    total_change = _clamp(annual_trend * config.horizon_years, -margin.max_total_change, margin.max_total_change)
    # S-1(2026-08-26修正、docs/model_audit_v4_2026-08-26.md):`floor` は「外挿の暴走を
    # 防ぐ下限」のはずが、現在の粗利率が floor を下回る銘柄では外挿結果を
    # 上から押し上げる**加点装置**になっていた(実データでAMR:粗利率11.2%→1.2%の
    # 崩壊が margin_multiple 4.29倍の改善として計上されていた)。floor は現在値
    # より上へ持ち上げてはならない(`lower_bound` は上で決めてある)。
    terminal = _clamp(current + total_change, lower_bound, margin.ceiling)
    # S-2:絶対ポイントの上限だけでは、粗利率が薄い銘柄ほど同じ改善幅が過大な
    # 倍率になる。終端粗利率が現在値の `max_relative_change` 倍を超えないように
    # 相対上限をかける(既定は実質無効)。
    return min(terminal, current * margin.max_relative_change)


def growth_fade_multiple_change(
    initial_growth: float, terminal_growth: float, config: ScoringConfig
) -> float:
    """終端マルチプル ÷ 現在マルチプル。**成長の減衰と整合させるためだけ**に動かす。

    ---------------------------------------------------------------------------
    **v3の平均回帰を撤廃した理由(28.2)。これがv4で最も重要な変更である。**

    v3は「現在のEV/粗利をセクター中央値へ対数空間で50%寄せる」としていた。
    15.1③「マルチプル再評価は低評価から入ることでのみ得られる」を平均回帰と
    して実装したものだったが、これは**モデルが市場より正しい適正倍率を知って
    いる**という主張であり、その根拠は無かった。

    実測(28.1、9評価日・7,024観測)では、この項の順位ICは **−0.023(t=−3.1、
    9日中7日で負)**——つまり順位を**悪化させる**方向に働いていた。機序は
    はっきりしている。EV/粗利の分子(EV)は今日の値、分母(粗利)は最大15ヶ月前の
    決算である。**株価が下がった銘柄は、業績悪化がまだ決算に出ていないだけで
    「割安」に見え**、そこへ平均回帰を適用すると、悪化が数字に現れる前の銘柄を
    系統的に買い上げることになる。15.3が警告した「成長しない万年割安株」を、
    v2とは別の経路で再現していた。

    **v4の立場:マルチプルの適正水準について、モデルは市場に対する優位を
    主張しない。** 現在の倍率は、その企業の現在の成長率・質・リスクについて
    市場が到達した集合的な評価であり、モデルの推定より情報量が多い。したがって
    `E[M_H] = M_0` を出発点に置き、**そこから動かしてよいのは、モデル自身が
    別途仮定している変化と整合させるためだけ**とする。

    ---------------------------------------------------------------------------
    **では何と整合させるのか——「成長は既に価格に入っている」。**

    モデルは売上を7年間 `growth_path` に沿って外挿する。初期成長率40%の企業は
    7年目には3%台まで減速している。**40%成長の企業に付く倍率と、3%成長の企業に
    付く倍率は同じではない。** 現在の倍率 M_0 は「40%で成長している企業」への
    値づけであり、その成長が終わったあとも同じ倍率が付く前提は、成長の対価を
    **二重に受け取る**ことになる。

    断面の値づけ構造から、この対価の大きさは直接測れる:

        ln(EV/粗利) = 定数 + κ · g   ……  κ = マルチプルの成長弾力性

    実データ(2024-04〜2025-08の9断面、各700銘柄弱)で推定した κ は
    **+0.863(価格ヒストリー全期間の20断面で +0.681〜+1.098、標準偏差0.117、
    t値は各断面 +3.5〜+6.3)** だった(28.2)。この κ を使えば、成長が g_0 から
    g_H へ減速したときの整合的なマルチプル変化は

        ln(M_H / M_0) = κ · (g_H − g_0)

    となる。**リターンには一切フィットさせていない構造パラメータ**である点が
    重要で、これは較正ではなく測定である。

    この形の帰結:
    - 高成長銘柄は売上倍率で大きく稼ぐが、マルチプル圧縮でその一部を失う
      (g0=50%なら圧縮0.66倍)。成長の対価を二重取りしなくなる
    - 低成長銘柄は圧縮をほとんど受けない(g0=10%なら0.94倍)
    - **「割安だから上がる」という無償のリレーティングは、どの銘柄にも
      発生しない**。これがv3で最も害の大きかった経路だった

    縮小している企業(g_0 < 終端成長率)では逆に g_H > g_0 となり倍率は拡大
    するが、その場合は売上倍率が1を大きく下回るため、`min_expected_moic` で
    ランキングから外れる。
    """
    mult = config.multiple
    change = math.exp(mult.growth_elasticity * (terminal_growth - initial_growth))
    return _clamp(change, mult.min_change, mult.max_change)


def dilution_drag_factor(dilution_rate: float, config: ScoringConfig) -> float:
    """7年後の株式数倍率(希薄化ドラッグ)。**増資と自社株買いを対称に扱わない**(30.4)。

        drag = Π_t (1 + r_t),   r_t = r                       (r >= 0:増資)
                                r_t = r × persistence^(t−1)   (r < 0:自社株買い)

    **なぜ非対称なのか。** 増資は資金需要という構造から生じる——赤字で成長する
    企業は資金が要るから発行するのであり、その必要は翌年も残る。一方、自社株
    買いは経営の裁量的な資本配分であり、しかも景気循環に対して順張りである
    (自社株買いは利益とキャッシュフローがピークにある年に増え、不況期に真っ先に
    止まる)。過去の買い戻しペースをそのまま7年複利で効かせると、下限
    (−5%/年)に張り付いた銘柄に無償で 1/(0.95^7) = 1.43倍 を配ることになる。

    実データ(2026-08-28)では、ランキング上位50の14%・51〜200位の20%が
    この下限に張り付いていた。

    `buyback_persistence = 1.0` なら従来どおり(減衰なし)。
    """
    horizon = config.horizon_years
    persistence = config.dilution.buyback_persistence
    if dilution_rate >= 0 or persistence >= 1.0:
        return (1 + dilution_rate) ** horizon
    drag = 1.0
    for t in range(horizon):
        drag *= 1 + dilution_rate * (persistence**t)
    return drag


def residual_reverted_multiple_change(
    current_multiple: float,
    initial_growth: float,
    terminal_growth: float,
    cross_section: CrossSection,
    config: ScoringConfig,
) -> float:
    """終端マルチプル ÷ 現在マルチプル。**成長調整後の割高・割安**を一部だけ戻す(30.5)。

        ln M_0 = c + κ·g_0 + ε
        ln M_H = c + κ·g_H + (1 − w)·ε
        ⇒ ln(M_H / M_0) = κ·(g_H − g_0) − w·ε

    `w = 0`(既定)なら `growth_fade_multiple_change` と完全に一致する。

    ---------------------------------------------------------------------------
    **v3 の平均回帰との違い。ここを取り違えると28.2の失敗を再現する。**

    v3 は現在のEV/粗利を**セクター中央値**へ寄せていた。セクター中央値は成長率を
    見ていないので、「成長しないから安い」銘柄が一律に買い上げられ、順位IC
    −0.023(t=−3.1)という実測の害が出た。

    ここで寄せる先は**その銘柄自身の成長率で説明される水準** `c + κ·g` である。
    成長率が低い銘柄の適正倍率は低く見積もられるので、「万年割安株」は
    寄せる先も低く、無償のリレーティングは発生しない。動くのは
    **同じ成長率の銘柄と比べて割高/割安な分(ε)だけ**である。

    ---------------------------------------------------------------------------
    **なぜこの項が必要か(2026-08-29の実測)。**
    現行モデルのランキングは、入口のバリュエーションと **正の** 相関を持つ:
    `corr(probability, ln(EV/粗利)) = +0.118`、`corr(ln E[MOIC], ln(EV/粗利)) = +0.196`
    (2026-08-28、ランキング716銘柄)。同じ事業・同じ成長を EV/粗利 5倍で買う
    のと 50倍で買うのとで、モデルの期待倍率が**同じどころか後者のほうが高い**。
    これは恒等式の問題ではなく、`E[M_H] = M_0` という仮定が入口価格を
    リターンから完全に切り離してしまうために起きる。

    ε の持続を仮定するのはモデルの謙虚さだが、`w = 0` は
    「割高さは永久に続く」という**それ自体が強い主張**でもある。
    w は 0〜1 の連続量なので、実測で選べばよい。
    """
    mult = config.multiple
    base_change = growth_fade_multiple_change(initial_growth, terminal_growth, config)
    weight = mult.residual_reversion_weight
    intercept = cross_section.log_multiple_intercept
    if weight <= 0 or intercept is None or current_multiple <= 0:
        return base_change
    residual = math.log(current_multiple) - (intercept + mult.growth_elasticity * initial_growth)
    change = math.exp(
        mult.growth_elasticity * (terminal_growth - initial_growth) - weight * residual
    )
    return _clamp(change, mult.min_change, mult.max_change)


def health_index(inputs: MoicInputs) -> float:
    """財務健全性を −1(危険)〜 +1(強固)に写した指標。算出できた項目の平均。

    旧v2は「財務健全性」を9%の重みを持つ独立した加点軸にしていたが、
    これは誤りだった。財務健全性は**リターンを増やす因子ではなく、7年後まで
    生き残ってリターンを実現できるかどうかの確率**である。ここでは加点ではなく
    `survival_probability` への入力として扱う(27.4)。

    算出できる項目が1つも無ければ0(中立)を返す。
    """
    components: list[float] = []

    if inputs.piotroski_ratio is not None:
        components.append(_clamp((inputs.piotroski_ratio - 0.5) * 2, -1.0, 1.0))

    if inputs.cash_runway_quarters is not None:
        if math.isinf(inputs.cash_runway_quarters):
            components.append(1.0)
        elif inputs.cash_runway_quarters > 0:
            # 6四半期(ゲートの下限)を0、24四半期を+1、1.5四半期を−1にする対数尺度
            components.append(_clamp(math.log(inputs.cash_runway_quarters / 6.0) / math.log(4.0), -1.0, 1.0))
        else:
            components.append(-1.0)

    if inputs.equity_to_assets is not None:
        components.append(_clamp((inputs.equity_to_assets - 0.35) / 0.35, -1.0, 1.0))

    if inputs.fcf_margin is not None:
        components.append(_clamp(inputs.fcf_margin / 0.15, -1.0, 1.0))

    if not components:
        return 0.0
    return sum(components) / len(components)


def survival_probability(health: float, config: ScoringConfig) -> float:
    """ホライズン終端まで生き残る確率。年間ハザード率の複利。

    `hazard = base * exp(-sensitivity * health)`。既定値(base=0.06,
    sensitivity=1.2)では health=0 で7年生存65%、health=+1 で88%、
    health=−1 で21%。小型株の実際の上場廃止率の実勢に合わせた事前値であり、
    バックテストでの較正対象。

    **この項が右裾を削ることは分かっている(28.6)**。実測では財務健全性が最も
    低いデシルほど「1年で2倍」の達成率が高い(15.1% vs 最上位デシル5.8%)。
    脆弱な企業は上下どちらの裾も厚い。それでも生存確率を掛けるのは、7年という
    ホライズンで**上場廃止したら10倍は実現しようがない**からであり、実測でも
    生存項を入れたほうが上位デシルの破綻率が下がる(1年 1.30→1.09、
    2年 1.01→0.89、いずれもユニバース比)。裾の厚さのうち**上向きの分**は
    σ が拾い、**下向きの分**をこの項が拾う、という役割分担にしている。
    """
    hazard = _clamp(
        config.survival.base_annual_hazard * math.exp(-config.survival.health_sensitivity * health),
        _MIN_ANNUAL_HAZARD,
        _MAX_ANNUAL_HAZARD,
    )
    return (1 - hazard) ** config.horizon_years


def raw_log_moic_sigma(
    inputs: MoicInputs,
    growth_rates: list[float],
    fade: float,
    leverage_effect: float,
    health: float,
    config: ScoringConfig,
) -> float:
    """log-MOIC の標準偏差(**縮小推定の前**)。独立な不確実性源を二乗和で合成する。

    最大の項は成長率の不確実性で、デルタ法で伝播させる:

        d/dg0 [ Σ ln(1+g_t) ] = Σ fade^t / (1 + g_t)

    過去の年次成長率のばらつきが大きい銘柄ほど、この係数を通じて7年後の
    推定が広がる。**これが旧v2の「成長安定性CV」を置き換える**。安定した成長を
    加点するのではなく、不安定な成長は推定を広げる。

    **レバレッジの伝播(27.13)**:上の項はいずれも**事業価値(EV)**の不確実性で
    あり、株主が受け取るのはそれを負債で割った残余である。`leverage_effect`
    (= 株主価値倍率 ÷ EV倍率)はまさにEVの変動が株主価値へ増幅されて伝わる
    倍率なので、そのまま標準偏差に掛かる。

    **財務脆弱性の伝播(28.6)**:健全性が低い企業は事業そのもののばらつきが
    大きい。実測でも健全性の最下位デシルは「1年で2倍」も「−50%以下」も同時に
    最も多い。`health_sigma_sensitivity` はこの両側の厚さを σ に載せる。
    生存確率(下向き)だけを掛けて σ(両側)を据え置くと、脆弱な企業の上向きの
    裾を系統的に過小評価することになる。
    """
    unc = config.uncertainty
    fragility = math.exp(-unc.health_sigma_sensitivity * health)
    return _clamp(
        _asset_sigma(inputs, growth_rates, fade, config) * max(leverage_effect, 1.0) * fragility,
        unc.min_total_sigma,
        unc.max_total_sigma,
    )


def _asset_sigma(
    inputs: MoicInputs, growth_rates: list[float], fade: float, config: ScoringConfig
) -> float:
    """レバレッジ・脆弱性を掛ける**前**の事業価値側の σ。ホライズン依存はここに集まる。"""
    unc = config.uncertainty
    volatility = _clamp(
        inputs.revenue_growth_volatility
        if inputs.revenue_growth_volatility is not None
        else unc.default_growth_volatility,
        unc.min_growth_volatility,
        unc.max_growth_volatility,
    )
    sensitivity = sum(fade ** (t + 1) / (1 + g) for t, g in enumerate(growth_rates) if g > -1)
    sigma_growth = volatility * sensitivity
    return math.sqrt(
        sigma_growth**2 + unc.multiple_sigma**2 + unc.margin_sigma**2 + unc.dilution_sigma**2
    )


def sigma_center_horizon_scale(
    inputs: MoicInputs,
    initial_growth: float,
    fade: float,
    config: ScoringConfig,
    reference_horizon_years: int,
) -> float:
    """σ の縮小中心を、断面を測ったホライズンから現在のホライズンへ引き直す倍率。

    **なぜ必要か(2026-08-26に発見した欠陥)**。`shrink_log_moic_sigma` は σ を
    断面の幾何平均へ 85% 寄せる。その中心は**スコアリング時のホライズン(既定7年)
    で測った値**であり、`scores.inputs.cross_section` にそのまま保存される。
    ところが27.24の「何年で何倍」の読み替えは、保存済み入力を**別のホライズンで
    計算し直す**。σ はホライズンとともに伸びるのに、中心だけが7年のまま据え置か
    れていたため、短いホライズンでは σ が中心へ**引き上げられて**いた。

    実測(合成ユニバース12銘柄):3年へ読み替えると σ は 0.667(正しい中心で
    計算し直した値)ではなく 0.740 になり、P(3年で3倍)が 4.76% → 5.70% と
    **2割過大**に出ていた。READMEが「保存済みの入力からその年数で計算し直す」
    「厳密に再計算」と書いている以上、これは表示された数字が主張と食い違う欠陥。

    断面全体をリクエストごとに組み直すのが厳密だが、詳細画面は1銘柄しか読まない。
    代わりに「中心はこの銘柄と同じ比率でホライズンに反応する」と仮定して倍率を
    掛ける。σ のホライズン依存は成長率不確実性の伝播係数
    (`Σ fade^t/(1+g_t)`)にほぼ集約されており、レバレッジ・脆弱性・
    マルチプル等の定数項は銘柄内で共通に効くため、この近似の誤差は小さい
    (上の例で σ 0.667 に対し本手法は 0.666)。
    """
    if reference_horizon_years <= 0 or reference_horizon_years == config.horizon_years:
        return 1.0
    reference_config = config.model_copy(update={"horizon_years": reference_horizon_years})
    current = _asset_sigma(inputs, growth_path(initial_growth, fade, config), fade, config)
    reference = _asset_sigma(
        inputs, growth_path(initial_growth, fade, reference_config), fade, reference_config
    )
    if reference <= 0:
        return 1.0
    return current / reference


def shrink_log_moic_sigma(
    raw_sigma: float,
    cross_section: CrossSection,
    config: ScoringConfig,
    center_scale: float = 1.0,
) -> float:
    """σ を断面の中心へ縮小する(28.4)。対数空間で `sigma_shrinkage` の重みで寄せる。

    **なぜ縮小するのか。** σ は上の4項を二乗和したものだが、そのうち銘柄ごとに
    実際に変わるのは(a)過去の年次成長率のばらつきと(b)レバレッジ倍率だけで、
    残り(マルチプル・利益率・希薄化)は全銘柄共通の定数である。しかも(a)は
    **年次が4期以上ある銘柄でしか算出できず、実データでの充足率は33%**——
    残り3分の2は既定値0.30が入る。つまり σ の断面のばらつきは、大半が
    「測れたか測れなかったか」というデータ充足の差でしかない。

    対数正規の閾値超過確率は σ に対して極めて敏感である。誤差の大きい推定量の
    ばらつきをそのまま確率に通すと、**順位が σ の推定ノイズに支配される**。
    誤差の大きい推定量を母集団の中心へ寄せるのは縮小推定(James–Stein)の
    標準的な扱いであり、実測でも縮小を強めるほどデシル単調性・IC・上位デシルの
    破綻率がすべて改善した(28.4の表)。

    **帰結として v4 の順位は実質的に「生存確率で調整した期待倍率」の順序に
    近い**(既定 0.85 では σ の断面差が15%しか残らない)。これは隠すべき事実
    ではなく、**現在のデータで σ の銘柄差を主張できるだけの根拠が無い**という
    正直な状態表明である。年次期数が伸びて(a)の充足率が上がれば、この値を
    下げて銘柄差を復活させればよい。
    """
    unc = config.uncertainty
    center = cross_section.median_log_sigma
    if unc.sigma_shrinkage <= 0 or center is None or center <= 0 or raw_sigma <= 0:
        return _clamp(raw_sigma, unc.min_total_sigma, unc.max_total_sigma)
    # 断面を測ったホライズンと今のホライズンが違う場合の引き直し
    # (`sigma_center_horizon_scale` 参照)。既定の目標なら 1.0 で素通りする。
    center = center * center_scale if center_scale > 0 else center
    blended = math.exp(
        (1 - unc.sigma_shrinkage) * math.log(raw_sigma) + unc.sigma_shrinkage * math.log(center)
    )
    return _clamp(blended, unc.min_total_sigma, unc.max_total_sigma)


def size_prior(market_cap: float, config: ScoringConfig) -> float:
    """規模の事前分布。**既定では無効(exponent = 0)**。

    「MOICの算術は規模に対して中立だが、現実の10倍達成率は規模とともに下がる」
    という仮説を担う項として v3 で導入したが、**実データはこれを支持しなかった**
    (28.7)。1年・2年どちらのホライズンでも、この項を入れると順位IC・デシル
    単調性・上位デシルのリフトがいずれも悪化した(1年で lift 1.245→1.208、
    2年で 1.141→1.076)。時価総額デシル別のオンペース率もほぼ平坦で、
    最小デシルはむしろ破綻率が最悪(25.4% vs 全体10%)だった。

    v4ではこの項が担おうとしていた機構——「大きくなった企業は高い成長マルチプルを
    維持できない」——を、成長フェードのマルチプル圧縮(28.2)が構造的に引き受けて
    いる。二重計上を避ける意味でも既定は 0 とする。

    設定だけは残してある。母集団や期間が変われば結論も変わりうるためであり、
    `exponent` を上げれば復活する。ただし**変更したら必ず `run-backtest` で
    KPIの変化を確認すること**。
    """
    prior_config = config.size_prior
    if prior_config.exponent <= 0 or market_cap <= 0:
        return 1.0
    ratio = prior_config.reference_market_cap_usd / market_cap
    return _clamp(ratio**prior_config.exponent, _MIN_SIZE_PRIOR, _MAX_SIZE_PRIOR)


def projected_net_debt(
    inputs: MoicInputs,
    growth_rates: list[float],
    terminal_margin: float,
    dilution_rate: float,
    config: ScoringConfig,
) -> float:
    """D-6(docs/defect_and_edge_audit_2026-08-28.md):ホライズン終端のネットデット。

        net_debt_H = net_debt_0
                     - Σ_t FCF_t                       # 営業からの純増減
                     - Σ_t (dilution_rate × その年の時価総額)  # 増資による調達

    `FCF_t` は `fcf_margin` を売上に掛ける。`fcf_margin` は `balance_sheet.fcf_margin_fade`
    で幾何的に 0 へ寄せる(赤字企業が永久に同率で燃え続ける前提を避ける)。
    増資は過去の希薄化ペース(`dilution_rate`)が続くとみなし、その調達額を現金へ
    戻す(D-6 の「二重計上に注意」——`dilution_drag` が株数側を、これが貸借対照表側を
    罰する。増資で現金が補充される分を無視すると過小になるので整合させる)。

    変化幅は `market_cap × (max_net_cash_credit_years / horizon_years)` に丸める。
    """
    bs = config.balance_sheet
    fcf_margin_0 = inputs.fcf_margin if inputs.fcf_margin is not None else 0.0
    revenue = inputs.revenue_latest
    market_cap = inputs.market_cap
    cumulative_fcf = 0.0
    cumulative_raise = 0.0
    for t, g in enumerate(growth_rates):
        revenue *= 1 + g
        market_cap *= 1 + g  # 時価総額は概ね売上に比例して伸びるとみなす
        fcf_margin_t = fcf_margin_0 * (bs.fcf_margin_fade ** (t + 1))
        cumulative_fcf += revenue * fcf_margin_t
        if dilution_rate > 0:
            cumulative_raise += dilution_rate * market_cap

    projected = inputs.net_debt - cumulative_fcf - cumulative_raise
    max_change = inputs.market_cap * (bs.max_net_cash_credit_years / config.horizon_years)
    return _clamp(projected, inputs.net_debt - max_change, inputs.net_debt + max_change)


def compute_moic(
    inputs: MoicInputs,
    cross_section: CrossSection,
    config: ScoringConfig,
    enforce_min_expected_moic: bool = True,
) -> MoicResult | None:
    """1銘柄の P(MOIC >= target) を計算する。ランキングできなければ None。

    None を返すのは「スコアが低い」ではなく「**順位を付けるべきでない**」という
    意味であり、理由は2種類ある。

    1. **測れない** — 必須入力の欠損、粗利が無い、EVがマイナス、株式がEVの
       ごく一部しか占めない超高レバレッジ
    2. **見通しがマイナス** — 測れたが期待倍率が `min_expected_moic` 未満(27.17)

    呼び出し元が2つを区別できるように、`enforce_min_expected_moic=False` で
    呼ぶと2番目の判定をスキップして結果を返す。区別が要るのは、実データで
    ランキング外375銘柄のうち267件(71%)が2番目に該当しており、これを
    「データ不足」と表示するのは**誤情報**になるためである(27.20)。
    """
    requirements = config.requirements
    if inputs.revenue_latest <= 0 or inputs.gross_profit_latest < requirements.min_gross_profit_usd:
        return None
    if inputs.market_cap <= 0 or inputs.gross_margin_latest <= 0:
        return None

    enterprise_value = inputs.market_cap + inputs.net_debt
    if enterprise_value <= 0:
        # ネットキャッシュが時価総額を上回る(EVがマイナス)。EV倍率が定義できない。
        return None
    if inputs.market_cap / enterprise_value < requirements.min_equity_share_of_ev:
        # 27.13:株式がEVのごく一部しか占めない超高レバレッジ銘柄。株式は実質的に
        # 負債に対するコールオプションであり、対数正規のMOICモデルは成立しない
        # (中央値の点推定にJensenの不等式が強く効く)。「測れない」として扱う。
        return None

    statement_growth = base_initial_growth(inputs, config)
    if statement_growth is None:
        return None
    # 30.1:循環性割引 → 28.3:価格ナウキャスト、の順。決算から出した推定量の
    # バイアスを先に直し、そのうえで決算の陳腐化を価格で埋める。
    base_growth = damp_growth_for_cyclicality(statement_growth, inputs, config)
    cyclicality_adjustment = base_growth - statement_growth
    initial_growth, nowcast_adjustment = nowcast_initial_growth(
        base_growth, inputs, cross_section, config
    )

    # --- ① 売上の成長(15.1①・28.10) -------------------------------------------
    fade = growth_fade(inputs, config)
    growth_rates = growth_path(initial_growth, fade, config)
    revenue_multiple = math.prod(1 + g for g in growth_rates)
    if revenue_multiple <= 0:
        return None

    # --- ② 利益率の変化(15.1②) -----------------------------------------------
    terminal_margin = terminal_gross_margin(inputs, config)
    margin_multiple = terminal_margin / inputs.gross_margin_latest

    # --- ③ マルチプルの変化(15.1③・28.2) -------------------------------------
    # 「割安だから上がる」ではなく「今の価格は今の成長率を既に織り込んでいる」。
    current_multiple = enterprise_value / inputs.gross_profit_latest
    terminal_growth = growth_rates[-1]
    multiple_change = residual_reverted_multiple_change(
        current_multiple, initial_growth, terminal_growth, cross_section, config
    )
    final_multiple = current_multiple * multiple_change
    # 30.3:終端 EV/粗利の絶対上限。7年後に終端成長率まで減速した事業が、
    # 今日の断面の最上位より高い倍率で評価され続ける前提は取らない。
    # 上限に当たった分は `multiple_change` の診断値にも反映する。
    terminal_multiple_capped = False
    cap = cross_section.ev_to_gross_profit_cap
    if cap is not None and cap > 0 and final_multiple > cap:
        final_multiple = cap
        multiple_change = final_multiple / current_multiple
        terminal_multiple_capped = True

    terminal_gross_profit = inputs.gross_profit_latest * revenue_multiple * margin_multiple
    terminal_ev = terminal_gross_profit * final_multiple

    # --- ④ 希薄化(15.1④) -----------------------------------------------------
    # (D-6:射影ネットデットが `dilution_rate` を必要とするため、レバレッジ計算より
    #  前に出した。挙動は変わらない。)
    dilution = config.dilution
    # A-1(2026-08-26、docs/model_audit_v4_2026-08-26.md):`dilution_cagr` が欠損して
    # いる銘柄を「希薄化ゼロ(=自社株買いと同等の最良シナリオ)」として扱うと、
    # 27.1の「欠損を減点に読み替えない」方針の裏返しで「欠損を満点に読み替える」
    # ことになっていた。断面の中央値(=典型的な希薄化ペース)を中立値として使い、
    # 断面統計が無い場合(1周目のCrossSection構築時等)だけ0.0にフォールバックする。
    dilution_data_missing = inputs.dilution_cagr is None
    if inputs.dilution_cagr is not None:
        dilution_source = inputs.dilution_cagr
    elif cross_section.median_dilution_cagr is not None:
        dilution_source = cross_section.median_dilution_cagr
    else:
        dilution_source = 0.0
    dilution_rate = _clamp(dilution_source, dilution.min_annual_rate, dilution.max_annual_rate)
    # 30.4:増資は持続、自社株買いは減衰。`dilution_drag_factor` を参照。
    dilution_drag = dilution_drag_factor(dilution_rate, config)
    if dilution_drag <= 0:
        return None

    # --- D-6:終端ネットデットの射影(既定は診断のみ) --------------------------
    projected_nd = projected_net_debt(inputs, growth_rates, terminal_margin, dilution_rate, config)
    net_debt_change = projected_nd - inputs.net_debt
    effective_net_debt = projected_nd if config.balance_sheet.project_net_debt else inputs.net_debt

    # --- 有利子負債の影響:EV倍率から株主価値倍率へ ------------------------------
    # ネットデットは(既定では)名目で一定と仮定する。レバレッジのかかった企業では
    # EVの上昇が株主価値に増幅されて効き(逆に下落も増幅される)、
    # ネットキャッシュの企業では希釈される。EVのまま比較すると、この効果を
    # 丸ごと落としてしまう。増幅はリターンだけでなく**ばらつきにも**効くので、
    # `raw_log_moic_sigma` に同じ係数を渡す。
    terminal_equity = terminal_ev - effective_net_debt
    if terminal_equity <= 0:
        return None
    equity_moic = terminal_equity / inputs.market_cap
    ev_moic = terminal_ev / enterprise_value
    leverage_effect = equity_moic / ev_moic if ev_moic > 0 else 1.0

    expected_moic = equity_moic / dilution_drag
    if expected_moic <= 0:
        return None
    if enforce_min_expected_moic and expected_moic < requirements.min_expected_moic:
        # 27.17:中心的な見通しが株主価値を毀損する銘柄は10倍候補ではない。
        # 対数正規は期待値を固定したまま分散を広げると閾値超過確率を上げるため、
        # ここで止めないと「縮小していく事業だが、ばらつきが大きいので10倍も
        # ありうる」という、モデルが外れることに賭けた順位づけになる。
        return None

    # --- 不確実性を載せて閾値超過確率にする ------------------------------------
    # **点推定は期待値であって中央値ではない(27.14)**。ここまでの計算は各因子の
    # 中心的な見通しを掛け合わせたものなので、得られる `expected_moic` は分布の
    # **平均**に相当する。対数正規では E[X] = exp(mu + sigma^2/2) なので、平均を
    # 固定したまま分散を広げると**中央値は下がる**。したがって
    # mu = ln(E[X]) - sigma^2/2 とするのが正しい。
    #
    # これにより、リスク要因(レバレッジ・成長の不安定さ・財務脆弱性)は sigma を
    # 通じて**中央値を押し下げる**ように働く。一方で閾値超過確率そのものは
    # sigma = sqrt(2 ln(target/expected)) 付近まで**上がる**——これは対数正規の
    # 性質として正しく、経済的にも妥当である(同じ期待値ならボラティリティの
    # 高い資産のほうが10倍に届く確率は高い)。危険なのはこの性質が
    # **期待値そのものが1を下回る銘柄**に適用される場合だけなので、そちらは
    # `requirements.min_expected_moic` で構造的に排除している(27.17)。
    health = health_index(inputs)
    raw_sigma = raw_log_moic_sigma(inputs, growth_rates, fade, leverage_effect, health, config)
    sigma = shrink_log_moic_sigma(
        raw_sigma,
        cross_section,
        config,
        center_scale=sigma_center_horizon_scale(
            inputs, initial_growth, fade, config, cross_section.horizon_years
        ),
    )
    # D-7(docs/defect_and_edge_audit_2026-08-28.md):`expected_moic` は5因子の**中心的
    # 見通しの積**である。各因子が対数正規なら中央値の積は積の中央値であって
    # 平均ではない(平均には各因子に exp(σ_i²/2) が要るが掛けていない)。したがって
    # `expected_moic` は構造的に中央値側の量で、そこから −σ²/2 を引くのは根拠の
    # ない恒久的減額になりうる(σ≈1 で中央値が4割引き)。既定は現状維持("mean")。
    # D-1/D-2 修正後に `compare-configs` で calibration_error と rank_ic の変化を
    # 測って決める。予測:順位KPIはほぼ不変、calibration_error(現在 過小予測)が改善。
    if getattr(config.uncertainty, "point_estimate_interpretation", "mean") == "median":
        mu = math.log(expected_moic)
    else:
        mu = math.log(expected_moic) - sigma**2 / 2
    median_moic = math.exp(mu)
    z = (math.log(config.target_moic) - mu) / sigma
    conditional_probability = 1 - _NORMAL.cdf(z)

    survival = survival_probability(health, config)
    prior = size_prior(inputs.market_cap, config)
    probability = _clamp(conditional_probability * survival * prior, 0.0, 1.0)

    # --- 診断フラグ(2026-08-26追加、S-5/S-6/A-1、docs/model_audit_v4_2026-08-26.md) --
    growth_clamped = is_initial_growth_clamped(inputs, config)
    lease_share = None
    if inputs.lease_liability is not None and inputs.net_debt > 0:
        lease_share = _clamp(inputs.lease_liability / inputs.net_debt, 0.0, 1.0)

    return MoicResult(
        probability=probability,
        expected_moic=expected_moic,
        median_moic=median_moic,
        log_moic_mu=mu,
        log_moic_sigma=sigma,
        survival_probability=survival,
        size_prior=prior,
        revenue_multiple=revenue_multiple,
        margin_multiple=margin_multiple,
        multiple_change=multiple_change,
        leverage_effect=leverage_effect,
        dilution_drag=dilution_drag,
        initial_growth_rate=initial_growth,
        base_growth_rate=base_growth,
        statement_growth_rate=statement_growth,
        growth_cyclicality_adjustment=cyclicality_adjustment,
        growth_nowcast_adjustment=nowcast_adjustment,
        terminal_multiple_capped=terminal_multiple_capped,
        revenue_trend_consistency=inputs.revenue_trend_consistency,
        gross_margin_consistency=inputs.gross_margin_consistency,
        terminal_growth_rate=terminal_growth,
        growth_fade_rate=fade,
        terminal_gross_margin=terminal_margin,
        current_ev_to_gross_profit=current_multiple,
        target_ev_to_gross_profit=final_multiple,
        implied_terminal_ev=terminal_ev,
        health_index=health,
        raw_log_moic_sigma=raw_sigma,
        growth_rate_clamped=growth_clamped,
        dilution_data_missing=dilution_data_missing,
        lease_share_of_net_debt=lease_share,
        net_debt_data_missing=inputs.net_debt_data_missing,
        projected_net_debt=projected_nd,
        net_debt_change=net_debt_change,
    )


def moic_quantiles(
    log_mu: float,
    log_sigma: float,
    survival_probability: float,
    quantiles: "list[float] | tuple[float, ...]" = (0.10, 0.25, 0.50, 0.75, 0.90),
) -> dict[float, float]:
    """実現倍率(MOIC)の分位点(J-4、docs/investment_decision_gap_2026-08-29.md)。

    **混合分布として扱う**:確率 `1 - S` で結果は ≈0(倒産・上場廃止)、確率 `S` で
    対数正規 `LogNormal(log_mu, log_sigma)`。累積確率 `q <= 1 - S` の分位点は
    `0.0` を返し、それ以外は `exp(mu + sigma * Phi^-1((q - (1-S)) / S))` を返す。

    **この扱いを省くと、生存確率 0.6 の銘柄の P10 を対数正規だけで出してしまい、
    ダウンサイドを構造的に過小評価する。**

    分位点は**生の対数正規から出す**。較正(28.8)は閾値超過確率にしか掛かって
    いない単調写像なので分位点には適用できない——呼び出し元(UI)は「この幅は
    モデルの仮定によるもので実測で較正されていない」と明記すること。
    """
    failure_mass = max(0.0, min(1.0, 1.0 - survival_probability))
    survive = 1.0 - failure_mass
    out: dict[float, float] = {}
    for q in quantiles:
        if q <= failure_mass or survive <= 0.0:
            out[q] = 0.0
            continue
        conditional_q = (q - failure_mass) / survive
        if conditional_q >= 1.0:
            # 数値誤差で 1.0 を超えた場合の保険(Phi^-1(1)=+inf を避ける)。
            conditional_q = 1.0 - 1e-12
        z = _NORMAL.inv_cdf(conditional_q)
        out[q] = math.exp(log_mu + log_sigma * z)
    return out


def _log_multiple_intercept(
    inputs_list: list[MoicInputs], config: ScoringConfig
) -> float | None:
    """断面の値づけ線 `ln(EV/粗利) = c + κ·g` の切片 c(30.5)。

    κ は `multiple.growth_elasticity` に固定してあるので、推定するのは切片だけ
    でよい。`c = median(ln M_i − κ·g_i)` とする——最小二乗ではなく中央値を使う
    のは、EV/粗利が右に極端な裾を持つため(実データで最大869倍)。切片が
    数銘柄の外れ値で動くと、全銘柄の終端倍率がまとめてずれる。

    説明変数の g は `base_initial_growth`(モデルが実際に外挿する初期成長率の
    決算ベース値)を使う。ナウキャスト後の値を使うと、切片の推定に価格情報が
    二重に入る。
    """
    mult = config.multiple
    if mult.residual_reversion_weight <= 0:
        return None
    residuals: list[float] = []
    for inputs in inputs_list:
        if inputs.gross_profit_latest <= 0:
            continue
        enterprise_value = inputs.market_cap + inputs.net_debt
        if enterprise_value <= 0:
            continue
        growth = base_initial_growth(inputs, config)
        if growth is None:
            continue
        residuals.append(
            math.log(enterprise_value / inputs.gross_profit_latest) - mult.growth_elasticity * growth
        )
    if len(residuals) < 20:
        return None
    return statistics.median(residuals)


def _ev_to_gross_profit_cap(
    inputs_list: list[MoicInputs], config: ScoringConfig
) -> float | None:
    """終端 EV/粗利の上限を当日の断面から作る(30.3)。無効なら None。

    実データ(2026-08-28、ランキング716銘柄)の EV/粗利は中央値5.8、p90 15.6、
    p95 22.1、p99 53.6、最大869。現行モデルは終端倍率を
    `M_0 × exp(κ(g_H − g_0))` としか動かさないため、EV/粗利 99倍で入った銘柄は
    7年後も65倍のまま評価され、その水準を前提に終端株主価値が積み上がる。
    """
    mult = config.multiple
    if mult.terminal_cap_percentile <= 0:
        return None
    ratios = sorted(
        (i.market_cap + i.net_debt) / i.gross_profit_latest
        for i in inputs_list
        if i.gross_profit_latest > 0 and (i.market_cap + i.net_debt) > 0
    )
    if len(ratios) < 20:
        # 断面が薄いと分位点が不安定になる。上限を作らない(=無効)ほうが安全。
        return None
    index = min(len(ratios) - 1, max(0, int(round(mult.terminal_cap_percentile * (len(ratios) - 1)))))
    return ratios[index] * mult.terminal_cap_slack


def build_cross_section(
    inputs_list: list[MoicInputs], config: ScoringConfig
) -> CrossSection:
    """同一評価日の全銘柄から `CrossSection` を組み立てる(28.5)。

    2段階になるのは、σ の縮小中心が σ そのものの断面統計だからである。

    1. 12ヶ月対数リターンの中央値を出す(ナウキャストの基準線)
    2. その基準線で1度モデルを回し、返ってきた `raw_log_moic_sigma` の
       **幾何平均**を縮小の中心にする(対数を取って算術平均し、指数に戻す)

    2周目は呼び出し元が行う。`compute_moic` は `median_log_sigma is None` の
    とき縮小を行わないので、1周目はそのまま素の σ を返す。

    `enforce_min_expected_moic=False` で回すのは、σ の中心を推定する母集団を
    「ランキング対象」に限らないため。見通しがマイナスの銘柄も同じ推定誤差
    構造を持っており、母数が多いほど中心は安定する。
    """
    momenta = sorted(i.log_momentum_12m for i in inputs_list if i.log_momentum_12m is not None)
    median_momentum = statistics.median(momenta) if momenta else None

    # A-1: 希薄化の断面中央値。欠損銘柄への中立値として使う(compute_moic参照)。
    dilutions = sorted(i.dilution_cagr for i in inputs_list if i.dilution_cagr is not None)
    median_dilution = statistics.median(dilutions) if dilutions else None

    # 30.3:終端 EV/粗利の上限。当日の断面の分位点 × slack。
    ev_cap = _ev_to_gross_profit_cap(inputs_list, config)
    # 30.5:値づけ線 ln(EV/粗利) = c + κ·g の切片 c。
    intercept = _log_multiple_intercept(inputs_list, config)

    first_pass = CrossSection(
        median_log_momentum=median_momentum,
        sample_size=len(inputs_list),
        median_dilution_cagr=median_dilution,
        horizon_years=config.horizon_years,
        ev_to_gross_profit_cap=ev_cap,
        log_multiple_intercept=intercept,
    )
    sigmas: list[float] = []
    for inputs in inputs_list:
        result = compute_moic(inputs, first_pass, config, enforce_min_expected_moic=False)
        if result is not None and result.raw_log_moic_sigma > 0:
            sigmas.append(math.log(result.raw_log_moic_sigma))

    median_sigma = math.exp(statistics.fmean(sigmas)) if sigmas else None
    return CrossSection(
        median_log_momentum=median_momentum,
        median_log_sigma=median_sigma,
        sample_size=len(inputs_list),
        median_dilution_cagr=median_dilution,
        horizon_years=config.horizon_years,
        ev_to_gross_profit_cap=ev_cap,
        log_multiple_intercept=intercept,
    )
