"""Configuration loading and validation.

Config files are parsed into Pydantic models at import/startup time so a
malformed threshold (e.g. a typo turning ``0.30`` into ``30``) fails the
process immediately instead of silently producing garbage gates or scores
(18章 fail-fast principle).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigSchemaError(RuntimeError):
    """設定ファイルとコードのスキーマが噛み合わない(28.17)。

    **素の `ValidationError` と区別できるようにするために存在する。** APIの
    エラーハンドラは「設定が古いのではなくプロセスが古い」という定型の案内を
    出すが、その判定を `ValidationError` かどうかで行うと、**レスポンスモデルの
    検証失敗まで同じ案内になる**——実際に `CandidateDetail.factors` の型が
    合わずに失敗したとき、「APIプロセスを再起動してください」という無関係な
    指示が出ていた。原因の違うものに同じ対処を案内するのは、何も案内しないより悪い。
    """

    def __init__(self, path: Path, error: Exception) -> None:
        self.path = path
        self.error = error
        super().__init__(f"{path} の内容がコードのスキーマと一致しません: {error}")

    @property
    def fields(self) -> list[str]:
        """噛み合わなかった項目名。UIが「どこが」を出せるようにする。"""
        errors = getattr(self.error, "errors", None)
        if not callable(errors):
            return []
        return sorted({".".join(str(p) for p in e["loc"]) for e in errors()})


@dataclass(frozen=True)
class UniverseCeilings:
    """ある目標(何年で何倍)に対して有効な規模の上限(29章)。

    `UniverseConfig.ceilings_for_target` が返す。`target_moic` が緩いほど上限は
    緩む——「大きすぎる企業は算数上10倍になれない」(15.6)という除外の根拠が、
    **10倍という目標に依存している**ため。
    """

    market_cap_usd: float
    revenue_usd: float
    # 上限を導いた目標倍率。`widening_capped` が True のとき、これは指定された
    # 目標倍率ではなく `min_supported_target_moic` になる。
    target_moic: float
    # 指定された目標が母集団の materialize 範囲より緩く、上限がそこで頭打ちに
    # なったか。UIはこれが True のとき「この目標では母集団がこれ以上広がらない」
    # と明示する——黙って狭い母集団を返すと、利用者は上限が目標に追随している
    # と誤解したまま「該当が少ない」という結論を受け取る。
    widening_capped: bool


class UniverseConfig(BaseModel):
    """ユニバース(母集団)の定義。

    **規模の上限は目標倍率の関数である**(29章)。設定に書くのは次の2つで、
    ドル建ての実効上限はそこから導く——同じ量を2箇所に書くと必ず食い違うため。

    - `exit_*_ceiling_usd` … 目標を達成した**出口**時点の規模の上限
    - `min_supported_target_moic` … 母集団を materialize する最も緩い目標

        ある目標での上限 = exit_*_ceiling_usd ÷ max(目標倍率, min_supported)
    """

    market: str = "US"
    # 目標達成時点(出口)の規模の上限。既定の目標(7年で10倍)では
    # 35B/10 = 3.5B、30B/10 = 3B となり、29章以前の固定値をそのまま再現する。
    exit_market_cap_ceiling_usd: float = Field(gt=0)
    exit_revenue_ceiling_usd: float = Field(gt=0)
    # バッチ(apply-gates → run-scoring)が materialize する最も緩い目標。
    # これより緩い目標を指定しても母集団はここで頭打ちになる(`widening_capped`)。
    min_supported_target_moic: float = Field(gt=1)
    min_price_usd: float = Field(gt=0)
    min_daily_dollar_volume_usd: float = Field(gt=0)
    min_listed_quarters: int = Field(gt=0)
    excluded_sectors: list[str] = Field(default_factory=list)

    @property
    def market_cap_ceiling_usd(self) -> float:
        """materialize する母集団の時価総額上限(=最も緩い目標での上限)。"""
        return self.exit_market_cap_ceiling_usd / self.min_supported_target_moic

    @property
    def revenue_ceiling_usd(self) -> float:
        """materialize する母集団のTTM売上高上限(同上)。"""
        return self.exit_revenue_ceiling_usd / self.min_supported_target_moic

    def ceilings_for_target(self, target_moic: float) -> UniverseCeilings:
        """目標倍率 `target_moic` に対して有効な規模の上限(29章)。"""
        capped = target_moic < self.min_supported_target_moic
        effective_target = max(target_moic, self.min_supported_target_moic)
        return UniverseCeilings(
            market_cap_usd=self.exit_market_cap_ceiling_usd / effective_target,
            revenue_usd=self.exit_revenue_ceiling_usd / effective_target,
            target_moic=effective_target,
            widening_capped=capped,
        )


class RetryConfig(BaseModel):
    max_attempts: int = Field(gt=0)
    backoff_base_seconds: float = Field(gt=0)
    backoff_max_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _max_after_base(self) -> RetryConfig:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


class CircuitBreakerConfig(BaseModel):
    min_sample_size: int = Field(gt=0)
    failure_rate_threshold: float = Field(gt=0, lt=1)


class QuarantineConfig(BaseModel):
    consecutive_failure_threshold: int = Field(gt=0)
    retry_interval_days: int = Field(gt=0)
    # B-5(2026-08-26、docs/model_audit_v4_2026-08-26.md):yfinanceは404を例外として
    # 送出せず、空の`info`として返すことがある(`ticker.info`が内部でHTTPエラーを
    # 握りつぶす)。この場合こちらのコードは`PermanentFailure`ではなく
    # `EmptyResponseError`として扱うため、`tickers.delisted_at`が一度も
    # 設定されず、バックテストの生存バイアス(27.15)が解消不能な構造的問題に
    # 見えていた。だが`empty_response`が「resolves within days」なのか
    # 「実質delisted」なのかは1回の観測では区別できない。ここでは連続失敗が
    # この回数(quarantineのリトライ間隔で数えると数週間分)に達したら、
    # 一時的な欠落ではなく実質的な消失とみなして`delisted_at`を設定する。
    empty_response_delisted_threshold: int = Field(gt=0, default=15)


class CollectionConfig(BaseModel):
    # 24.7:旧`min_workers`は並列度の下限として定義されていたが、
    # ThreadPoolExecutorへは`max_workers`しか渡しておらず、一度も参照されない
    # 死んだ設定値だった(「3〜8で可変」という誤解を招く記述と実装の矛盾)。
    # 動的スケーリングは実装しておらず、常に`max_workers`固定で動作する。
    max_workers: int = Field(gt=0)
    request_jitter_min_seconds: float = Field(ge=0)
    request_jitter_max_seconds: float = Field(ge=0)
    # 2026-08-30:Yahooへの秒あたり上限。**ジッタだけでは上限にならない**——
    # ジッタは1銘柄の処理を始める前に1回入るだけなので、実際の送信レートは
    # 「Yahooが速く返すほど上がる」。詰まっていないときほど速く叩き、
    # 制限に触れた瞬間から急に失敗が増える、という一番まずい形になっていた。
    # ワーカー数とジッタ(実測で調整済み)はそのままに、秒あたりの天井を足す。
    yfinance_requests_per_second: float = Field(gt=0, default=2.0)
    retry: RetryConfig
    circuit_breaker: CircuitBreakerConfig
    quarantine: QuarantineConfig

    @model_validator(mode="after")
    def _jitter_range(self) -> CollectionConfig:
        if self.request_jitter_max_seconds < self.request_jitter_min_seconds:
            raise ValueError("request_jitter_max_seconds must be >= request_jitter_min_seconds")
        return self


class GrowthConfig(BaseModel):
    terminal_rate: float
    fade: float = Field(gt=0, lt=1)
    max_initial_rate: float
    min_initial_rate: float
    # 28.3:価格ナウキャストで初期成長率を修正する強さと、その上限(ポイント)。
    # 0 にすると財務諸表だけの推定に戻る。
    nowcast_weight: float = Field(ge=0, le=1, default=0.0)
    nowcast_cap: float = Field(ge=0, default=0.0)
    # S-8(2026-08-26、docs/model_audit_v4_2026-08-26.md):決算が縮小を示している
    # 銘柄(base_growth < 0)を成長企業側へ反転させる補正は、一次情報(決算)を
    # 株価で上書きする行為なので、通常の nowcast_cap より狭い上限を課す。
    # nowcast_cap 以上にはならない(min で丸める)。
    nowcast_cap_sign_flip: float = Field(ge=0, default=1.0)
    # 28.10:事業の質(Piotroski F-score)で減衰率を動かす強さと、その範囲。
    # 0 にすると全銘柄が `fade` 固定になる。
    fade_quality_sensitivity: float = Field(ge=0, default=0.0)
    min_fade: float = Field(gt=0, lt=1, default=0.55)
    max_fade: float = Field(gt=0, lt=1, default=0.92)
    # S-7(2026-08-26、docs/model_audit_v4_2026-08-26.md):3年CAGRと直近年次YoYの
    # どちらか一方しか無い銘柄に適用する、より保守的な初期成長率の上限。
    # 「食い違ったら遅いほうを信じる」安全装置(27.13)は観測が2つ揃って
    # はじめて働くため。既定 1.0 は実質無効(max_initial_rate 側で丸められる)。
    max_initial_rate_single_observation: float = Field(default=1.0)

    # 30.1:初期成長率の循環性割引。年次売上系列が上下に振れているだけの銘柄
    # (市況型)では、直近の成長率は「その企業の成長力」ではなく「循環のどの
    # 局面にいるか」を測っている。超過成長分を一致度で割り引いて終端成長率へ
    # 寄せる:`g_eff = terminal + (g0 − terminal) × (1 − damping × (1 − consistency))`
    # 0 で無効。
    cyclicality_damping: float = Field(ge=0, le=1, default=0.0)

    @model_validator(mode="after")
    def _range_ordered(self) -> "GrowthConfig":
        if self.min_initial_rate >= self.max_initial_rate:
            raise ValueError("min_initial_rate must be < max_initial_rate")
        if self.min_fade >= self.max_fade:
            raise ValueError("min_fade must be < max_fade")
        if not self.min_fade <= self.fade <= self.max_fade:
            raise ValueError("fade must lie within [min_fade, max_fade]")
        return self


class MarginConfig(BaseModel):
    trend_damping: float = Field(ge=0, le=1)
    max_total_change: float = Field(ge=0)
    # S-2(2026-08-26追加、docs/model_audit_v4_2026-08-26.md):絶対ポイントの上限
    # (max_total_change)だけでは、粗利率が薄い銘柄ほど同じ改善幅が過大な倍率に
    # なる(3.8%→11.6%は3.06倍、50%→57.8%は1.16倍)。終端粗利率が現在の何倍
    # までを許すかの相対上限。既定値は大きく取ってあり実質無効(既存設定を壊さない)。
    max_relative_change: float = Field(gt=1.0, default=100.0)
    floor: float = Field(gt=0, lt=1)
    ceiling: float = Field(gt=0, le=1)

    # 30.1:粗利率トレンドの循環性割引。
    # `effective_trend = trend × (1 − cyclicality_damping × (1 − consistency))`
    # consistency は年次粗利率系列の一方向性(`series_trend_consistency`)。
    # 0 で無効(=従来どおり直近2期の差分をそのまま外挿)、1 で完全に循環分を落とす。
    cyclicality_damping: float = Field(ge=0, le=1, default=0.0)

    @model_validator(mode="after")
    def _range_ordered(self) -> "MarginConfig":
        if self.floor >= self.ceiling:
            raise ValueError("margin floor must be < ceiling")
        return self


class MultipleConfig(BaseModel):
    """終端マルチプルの扱い(28.2)。

    v3にあった `mean_reversion_weight` / `upward_rerating_growth_spread` /
    `min_sector_sample` / `floor` / `ceiling` / `size_compression_*` は撤廃した。
    マルチプルをセクター中央値へ平均回帰させる仕組みそのものを削除したため、
    それを制御していた設定値は指す対象を失っている。
    """

    # 断面の値づけ構造 ln(EV/粗利) = 定数 + κ·g から推定した κ。
    # リターンにフィットさせるのではなく、バリュエーションの断面から測る。
    growth_elasticity: float = Field(ge=0)
    max_change: float = Field(gt=1)
    min_change: float = Field(gt=0, lt=1)

    # 30.3:終端 EV/粗利の絶対上限を、**当日のユニバース断面の分位点**から決める。
    # `cap = percentile(EV/粗利, terminal_cap_percentile) × terminal_cap_slack`
    # 0 で無効。v4 は「モデルは適正倍率について市場に優位を主張しない」という
    # 立場を取っているが(28.2)、それは**今日の倍率**についての話である。
    # 7年後、成長が終端成長率まで減速した事業が、今日の断面の最上位が付けている
    # 倍率をなお上回るという前提は、どの投資理論からも支持されない。
    terminal_cap_percentile: float = Field(ge=0, le=1, default=0.0)
    terminal_cap_slack: float = Field(gt=0, default=1.0)

    # 30.5:**成長調整後の割高・割安**(断面回帰の残差)を終端倍率へどれだけ
    # 反映させるか。0 で無効(=v4 のまま、残差は7年間そのまま残る)。
    #
    #   ln M_0 = c + κ·g_0 + ε      (断面の値づけ構造。c は当日の断面から測る)
    #   ln M_H = c + κ·g_H + (1 − w)·ε
    #
    # w = 0 は「その銘柄が同じ成長率の同業より高い/低い倍率で取引されている
    # 理由は7年後も同じだけ残る」という主張であり、v4 の既定の立場である。
    # **v3 が撤廃したセクター中央値への平均回帰とは別物である点に注意**:
    # v3 は成長率を無視してセクター中央値へ寄せていたため、「成長しないから
    # 安い」銘柄を買い上げていた(順位IC −0.023)。ここで寄せる先は
    # **その銘柄自身の成長率で説明される水準**であり、動くのは
    # 「同じ成長率の銘柄と比べて割高/割安な分」だけである。
    residual_reversion_weight: float = Field(ge=0, le=1, default=0.0)


class DilutionConfig(BaseModel):
    min_annual_rate: float
    max_annual_rate: float
    # 30.4:自社株買い(株式数の減少)の持続性。
    # 増資は資金需要という**構造**に駆動されるので持続しやすいが、自社株買いは
    # 経営の裁量的な資本配分であり、しかも景気循環に対して順張り(業績が良い年に
    # 買い、悪い年に止める)である。過去の買い戻しペースを7年間そのまま複利で
    # 効かせると、無償の倍率を配ることになる。負のレートにだけこの係数で
    # 幾何減衰を掛ける(1.0 で従来どおり=減衰なし)。
    buyback_persistence: float = Field(gt=0, le=1, default=1.0)

    @model_validator(mode="after")
    def _range_ordered(self) -> "DilutionConfig":
        if self.min_annual_rate >= self.max_annual_rate:
            raise ValueError("min_annual_rate must be < max_annual_rate")
        return self


class SurvivalConfig(BaseModel):
    base_annual_hazard: float = Field(gt=0, lt=1)
    health_sensitivity: float = Field(ge=0)


class UncertaintyConfig(BaseModel):
    default_growth_volatility: float = Field(gt=0)
    min_growth_volatility: float = Field(gt=0)
    max_growth_volatility: float = Field(gt=0)
    multiple_sigma: float = Field(ge=0)
    margin_sigma: float = Field(ge=0)
    dilution_sigma: float = Field(ge=0)
    min_total_sigma: float = Field(gt=0)
    max_total_sigma: float = Field(gt=0)
    # 28.6:財務健全性を σ に伝播させる強さ。0 で無効。
    health_sigma_sensitivity: float = Field(ge=0, default=0.0)
    # 28.4:σ を断面中心へ縮小する重み(対数空間)。1.0 で完全に潰す。
    sigma_shrinkage: float = Field(ge=0, le=1, default=0.0)
    # D-7(docs/defect_and_edge_audit_2026-08-28.md):点推定(expected_moic)を対数正規の
    # 「平均」とみなすか「中央値」とみなすか。"median" のとき mu = ln(expected_moic)
    # (−σ²/2 の減額をしない)。既定は現状維持の "mean"。D-1/D-2 修正後に
    # compare-configs で決める。
    point_estimate_interpretation: str = Field(default="mean", pattern="^(mean|median)$")

    @model_validator(mode="after")
    def _ranges_ordered(self) -> "UncertaintyConfig":
        if self.min_growth_volatility >= self.max_growth_volatility:
            raise ValueError("min_growth_volatility must be < max_growth_volatility")
        if self.min_total_sigma >= self.max_total_sigma:
            raise ValueError("min_total_sigma must be < max_total_sigma")
        return self


class SizePriorConfig(BaseModel):
    exponent: float = Field(ge=0)
    reference_market_cap_usd: float = Field(gt=0)


class BalanceSheetConfig(BaseModel):
    """D-6(docs/defect_and_edge_audit_2026-08-28.md):終端ネットデットの射影。

    現状 `terminal_equity = terminal_ev - net_debt` で **net_debt を7年間名目一定と
    仮定**しており、ネットキャッシュを持つ赤字マイクロキャップ(このアプリの中心
    プロファイル)を systematically 過大評価する。フラグだけ先に入れ、D-1/D-2 が
    直ってから `compare-configs` で採否を判定する(**既定は無効**)。
    """

    project_net_debt: bool = False
    # FCFマージンをホライズン終端へ寄せる幾何減衰(赤字企業が永久に同率で
    # 燃え続ける前提を避ける)。t年目の fcf_margin = fcf_margin_0 * fade**t。
    fcf_margin_fade: float = Field(gt=0, lt=1, default=0.75)
    # ネットキャッシュを「7年後も株主のもの」として戻せる上限年数。射影による
    # net_debt の変化幅を market_cap × (この年数 / horizon_years) に丸める。
    max_net_cash_credit_years: int = Field(gt=0, default=7)


class RequirementsConfig(BaseModel):
    min_annual_revenue_periods: int = Field(ge=2)
    min_gross_profit_usd: float = Field(gt=0)
    min_equity_share_of_ev: float = Field(gt=0, lt=1)
    min_expected_moic: float = Field(ge=1.0)


class CalibrationConfig(BaseModel):
    """生の確率を実測頻度へ写す較正層の設定(28.8)。"""

    enabled: bool = False
    # 較正曲線を学習するのに最低限必要なバックテスト観測数。
    # これを下回るときは較正せず、生の確率をそのまま出す。
    min_observations: int = Field(gt=0, default=1000)
    # 単調(等調)回帰を当てる前のビン数。細かすぎると各ビンの頻度がノイズになる。
    bins: int = Field(ge=3, le=50, default=10)


class KpiAcceptanceConfig(BaseModel):
    """A-5(docs/defect_and_edge_audit_2026-08-28.md D-3):14.2 の成功指標を機械可読に
    した受け入れ基準。

    D-3 の指摘は「下落回避KPIが符号だけ合っていて実質未達なのに、誰も落第と
    言っていない」だった。`BacktestMetrics.kpi_verdicts` が各KPIを
    PASS / FAIL / INSUFFICIENT_DATA で判定し、`run-backtest` は FAIL があれば
    非ゼロ終了する。

    **`min_effective_observations` を下回る評価日数しか無ければ全KPIが
    INSUFFICIENT_DATA になる。** D-1/D-2 が直るまで多くのKPIはここに落ちる想定で、
    それが正しい状態表明である。
    """

    min_lift_ratio: float = Field(gt=0, default=1.5)
    # 上位デシルの大幅下落率 ÷ ユニバース平均。1.0 未満(=上位デシルのほうが
    # 下落を回避できている)を要求する。D-3 実測は 0.974 でほぼ未達。
    max_top_decile_loss_ratio: float = Field(gt=0, default=0.8)
    min_decile_monotonicity: float = Field(ge=-1, le=1, default=0.7)
    max_abs_calibration_error: float = Field(ge=0, default=0.03)
    min_rank_ic: float = Field(default=0.03)
    # D-2:観測数加重後の実効的な評価日数がこれ未満なら判定を出さない。
    min_effective_dates: int = Field(gt=0, default=6)


class FreshnessConfig(BaseModel):
    """A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):スコアリングを走らせてよい
    データ鮮度の前提条件。

    日次収集が一斉隔離・レート制限・ネットワーク断で途中停止すると、`run_scoring`
    は前日以前の価格・財務で当日付の `scores` を書き続けてしまう(実際に
    2026-08-28 のランキングは 08-24 の株価で作られていた)。**古いランキングを
    新しい日付で出すより、中止するほうが安全**である。
    """

    # 最新の price_snapshots がこの営業日数より古ければスコアリングを中止する。
    max_price_staleness_days: int = Field(ge=0, default=2)
    # 当日ゲート通過銘柄のうち「最新の取引日の価格行を持つ」割合がこれを下回れば
    # 中止する(収集が途中で落ちた実行を検知する)。
    min_same_day_price_coverage: float = Field(ge=0, le=1, default=0.9)


class ScoringConfig(BaseModel):
    """実現時価総額倍率(implied MOIC)モデルの全パラメータ(27章・28章)。

    旧v2の `weights` / `coverage_floor` は廃止した。スコアは8軸の加重平均では
    なく、15.1の恒等式に沿った積として組み立てた P(MOIC >= target_moic)
    そのものであり、軸ごとの重みという概念を持たない。
    """

    scoring_version: str
    horizon_years: int = Field(gt=0, le=30)
    target_moic: float = Field(gt=1)
    growth: GrowthConfig
    margin: MarginConfig
    multiple: MultipleConfig
    dilution: DilutionConfig
    survival: SurvivalConfig
    uncertainty: UncertaintyConfig
    size_prior: SizePriorConfig
    requirements: RequirementsConfig
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    balance_sheet: BalanceSheetConfig = Field(default_factory=BalanceSheetConfig)
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    # A-5(docs/defect_and_edge_audit_2026-08-28.md D-3):14.2 の成功指標を機械可読に
    # した受け入れ基準。`run-backtest` が PASS/FAIL/INSUFFICIENT_DATA を判定する。
    kpi_acceptance: KpiAcceptanceConfig = Field(default_factory=lambda: KpiAcceptanceConfig())


class ModelV5ReliabilityConfig(BaseModel):
    """Phase 1 reliability contract; confidence never shifts the mean."""

    ready_input_confidence: float = Field(ge=0, le=1, default=0.5)
    unavailable_input_confidence: float = Field(ge=0, le=1, default=0.0)


class ModelV5UncertaintyConfig(BaseModel):
    """Controls for the explicit Phase 2 scenario-mixture distribution."""

    confidence_sigma_multiplier: float = Field(ge=0, default=0.5)
    left_tail_multiplier: float = Field(ge=1, default=1.25)
    scenario_log_shift_sigma: float = Field(ge=0, default=0.5)


class ModelV5GrowthConfig(BaseModel):
    """Conservative Phase 3 observation-update parameters.

    These are challenger parameters, not accepted production constants. Every
    update is bounded and persisted with a leave-one-feature-out impact.
    """

    consensus_revision_weight: float = Field(ge=0, le=1, default=0.35)
    operating_kpi_weight: float = Field(ge=0, le=1, default=0.20)
    guidance_weight: float = Field(ge=0, le=1, default=0.35)
    max_initial_growth_adjustment: float = Field(gt=0, le=0.5, default=0.08)
    min_initial_growth_rate: float = Field(gt=-1, lt=1, default=-0.50)
    max_initial_growth_rate: float = Field(gt=0, le=2, default=0.75)
    min_kpi_comparison_days: int = Field(ge=30, default=60)
    max_kpi_comparison_days: int = Field(gt=60, default=450)
    tam_min_headroom_ratio: float = Field(gt=1, default=1.01)
    ablation_enabled: bool = True

    @model_validator(mode="after")
    def growth_bounds_ordered(self) -> ModelV5GrowthConfig:
        if self.min_initial_growth_rate >= self.max_initial_growth_rate:
            raise ValueError("min_initial_growth_rate must be < max_initial_growth_rate")
        if self.min_kpi_comparison_days >= self.max_kpi_comparison_days:
            raise ValueError("min_kpi_comparison_days must be < max_kpi_comparison_days")
        return self


class ModelV5ScenarioWeights(BaseModel):
    downside: float = Field(ge=0, le=1)
    base: float = Field(ge=0, le=1)
    upside: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> ModelV5ScenarioWeights:
        if abs(self.downside + self.base + self.upside - 1.0) > 1e-9:
            raise ValueError("scenario weights must sum to 1")
        return self


class ModelV5Config(BaseModel):
    """Independent v5 shadow-model configuration (GitHub Issue #3 Phase 1)."""

    enabled: bool = True
    mode: Literal["shadow", "active", "legacy"] = "shadow"
    model_version: Literal["v5"] = "v5"
    implementation_version: str = Field(default="v5.phase3", pattern=r"^v5\.phase\d+$")
    target_horizon_years: int = Field(gt=0, le=30, default=7)
    target_moic: float = Field(gt=1, default=10.0)
    reliability: ModelV5ReliabilityConfig = Field(default_factory=ModelV5ReliabilityConfig)
    uncertainty: ModelV5UncertaintyConfig = Field(default_factory=ModelV5UncertaintyConfig)
    growth: ModelV5GrowthConfig = Field(default_factory=ModelV5GrowthConfig)
    scenario_weights: ModelV5ScenarioWeights
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class ObjectiveDefinition(BaseModel):
    enabled: bool = True
    description: str
    downside_lambda: float | None = Field(default=None, ge=0)
    right_tail_moic: float | None = Field(default=None, gt=1)


class ObjectivesConfig(BaseModel):
    default_objective: str
    objectives: dict[str, ObjectiveDefinition]

    @model_validator(mode="after")
    def default_is_enabled(self) -> ObjectivesConfig:
        definition = self.objectives.get(self.default_objective)
        if definition is None or not definition.enabled:
            raise ValueError("default_objective must name an enabled objective")
        return self


class RiskSizingConfig(BaseModel):
    """Display-only shrink factors; never expands the existing hard cap."""

    enabled: bool = False
    target_annual_vol: float = Field(gt=0, default=0.60)
    min_vol_factor: float = Field(gt=0, le=1, default=0.35)
    max_pairwise_corr_soft: float = Field(ge=0, le=1, default=0.65)
    evidence_grade_factors: dict[str, float] = Field(
        default_factory=lambda: {"A": 1.0, "B": 0.9, "C": 0.75, "D": 0.5}
    )


class PortfolioConfig(BaseModel):
    """ポジションサイジングの規律(30.2.2 / 30.7)。

    元文書 第11節「等金額・銘柄数を多く・上限を固定・小さいほうを採る」に対応する。
    **この値はモデルが決めるものではなく利用者が決めるもの**である。ケリー基準的な
    最適化を行わないのは、確率の絶対水準がまだ検証途上だから(27章・14.2)。
    """

    portfolio_value_usd: float = Field(gt=0)
    per_position_cap: float = Field(gt=0, le=1)
    binary_event_position_cap: float = Field(gt=0, le=1)
    adv_participation_cap: float = Field(gt=0, le=1)
    sector_cap: float = Field(gt=0, le=1)
    max_positions: int = Field(gt=0)
    risk_sizing: RiskSizingConfig = Field(default_factory=RiskSizingConfig)

    @model_validator(mode="after")
    def _caps_ordered(self) -> PortfolioConfig:
        if self.binary_event_position_cap > self.per_position_cap:
            raise ValueError("binary_event_position_cap must be <= per_position_cap")
        if self.sector_cap < self.per_position_cap:
            raise ValueError("sector_cap must be >= per_position_cap")
        return self


class ExecutionConfig(BaseModel):
    """取引コスト・約定モデルの設定(docs/defect_and_edge_audit_2026-08-28.md D-5 / I-7)。

    スプレッドは既存OHLCVから Corwin–Schultz で推定するため設定不要。ここに置くのは
    平方根則マーケットインパクトの係数と、口座固有の手数料・下限スプレッドだけ。
    """

    impact_coefficient: float = Field(ge=0, default=0.10)
    commission_bps: float = Field(ge=0, default=0.0)
    min_half_spread_bps: float = Field(ge=0, default=15.0)


class EdgarRetryConfig(BaseModel):
    max_attempts: int = Field(gt=0)
    backoff_base_seconds: float = Field(gt=0)
    backoff_max_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _max_after_base(self) -> EdgarRetryConfig:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


class EdgarConfig(BaseModel):
    """SEC EDGAR連携の設定(30.3.1)。`user_agent` 自体は `.env` の
    `EDGAR_USER_AGENT`(`Settings.edgar_user_agent`)から読む——連絡先メール
    アドレスを含むため git 管理下の yaml には置かない。"""

    enabled: bool = True
    requests_per_second: float = Field(gt=0, le=10)
    timeout_seconds: float = Field(gt=0)
    document_fetch_enabled: bool = True
    max_tracked_tickers: int = Field(gt=0)
    # 2026-08-30:429/503(`Retry-After` 指定が無い場合)と403(遮断)を受けた
    # ときに、SEC向けリクエスト**全体**を止める秒数。個別リクエストのバックオフ
    # (`retry`)とは別物で、こちらは残りの銘柄が叩き続けるのを防ぐためにある。
    throttle_cooldown_seconds: float = Field(gt=0, default=60.0)
    retry: EdgarRetryConfig


class FredConfig(BaseModel):
    """FRED(マクロ系列)の設定(30.8)。`api_key` は `.env` から読む。"""

    enabled: bool = True
    # J-10:DEXJPUS(ドル円)を追加。円換算表示に使う。FRED_API_KEY 未設定時は
    # API 側で yfinance の `JPY=X` にフォールバックする。
    series_ids: list[str] = Field(
        default_factory=lambda: ["DGS10", "DFII10", "BAMLH0A0HYM2", "DEXJPUS"]
    )


class LlmConfig(BaseModel):
    """K-9:Claude API(定性分析)の設定。`api_key` は `.env` から読む。

    **ここで作るものは一切ゲート(`screening/exclusion_gates.py`)にもスコア
    (`scoring/`)にも入らない**——`docs/outside_tenx_implementation_plan_2026-08-28.md`
    第618行の原則1「再現性が無く、検証もできない判定をブロッキング条件にしては
    ならない」を、コードの構造で守る。LLMの出力は同じ入力でも毎回変わりうるので、
    除外や順位づけの根拠にした瞬間、バックテストの再現性が失われる。
    保存先を `llm_analyses` に隔離し、用途を表示・ノート起草・人間の下読みに
    限定しているのはこのため(K-1の5テーブルと同じ扱い)。

    `max_input_chars` を**超えた入力は切り詰めずに失敗させる**。途中で切った
    10-Kを要約すると、「リスク要因の後半に何も無かった」のか「読ませていない」
    のかが出力から区別できなくなる——`Filing.analysis` が NULL と空dictを
    区別しているのと同じ理由で、黙って部分入力にするより落ちる方が良い。
    """

    enabled: bool = True
    # K-9(docs/ui_llm_provider_selection_2026-08-30.md):どのAPIに投げるか。
    # `anthropic`(既定)= Claude native。`openai_compat` = OpenAI互換 `/v1/chat/
    # completions`。ChatGPT / NVIDIA NIM / Ollama / vLLM / LM Studio / LiteLLM は
    # すべて後者で、`base_url` と APIキーの差し替えだけで切り替わる。
    provider: str = "anthropic"
    # `openai_compat` のエンドポイント。None なら OpenAI 本家(api.openai.com)。
    # 例: http://localhost:11434/v1(Ollama)、https://integrate.api.nvidia.com/v1(NIM)。
    base_url: str | None = None
    # `openai_compat` で `effort` を `reasoning_effort` として送るか。既定は送らない
    # ——互換サーバの多くは未知パラメータを 400 で弾くため。推論モデルを使うときだけ true。
    send_effort: bool = False
    # スキル既定。コスト都合で下げるのは人間の判断であり、コードの既定にはしない。
    model: str = "claude-opus-5"
    # 思考の深さ。low|medium|high|xhigh|max。既定の high は API 側の既定と同じ。
    effort: str = "high"
    max_output_tokens: int = Field(gt=0, default=8000)
    # 1回の呼び出しに入れる本文の上限(文字数)。約4文字=1トークンとして、
    # 120,000文字 ≒ 30kトークン ≒ 入力$0.15/回(claude-opus-5 の $5/1Mトークン)。
    # 超過分は切り詰めではなく失敗として数える(上のdocstring参照)。
    max_input_chars: int = Field(gt=0, default=120_000)
    # 1回のバッチ実行で扱う銘柄数の上限。EDGARの `max_tracked_tickers` と違い
    # こちらは**課金**が効く上限なので、既定は意図的に小さくしてある。
    max_tickers_per_run: int = Field(gt=0, default=25)
    # Batch API(定性サブスコア)のポーリング。SDK既定の最大24時間に合わせる。
    batch_poll_interval_seconds: float = Field(gt=0, default=30.0)
    batch_timeout_seconds: float = Field(gt=0, default=86_400.0)

    @model_validator(mode="after")
    def _known_effort(self) -> LlmConfig:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if self.effort not in allowed:
            raise ValueError(f"effort must be one of {sorted(allowed)}, got {self.effort!r}")
        return self

    @model_validator(mode="after")
    def _known_provider(self) -> LlmConfig:
        allowed = {"anthropic", "openai_compat"}
        if self.provider not in allowed:
            raise ValueError(f"provider must be one of {sorted(allowed)}, got {self.provider!r}")
        return self


class Position(BaseModel):
    """1件の保有銘柄(30.7.1)。**アプリはこのファイルを読むだけで、書かない**
    (30.1.1 原則2)。追加・売却は手で編集し、gitにコミットする。"""

    ticker: str
    opened_on: datetime.date
    shares: float = Field(gt=0)
    cost_basis_usd: float = Field(gt=0)
    note: str | None = None  # 省略時は research/<TICKER>.md を見る
    binary_event: bool = False
    closed_on: datetime.date | None = None


class MonitoringConfig(BaseModel):
    """`config/monitoring.yaml`(30.7.3)。閾値は売却条件ではなく、判断を
    やり直す合図——機械的な売りシグナルとして使ってはならない(元文書 第11節)。"""

    revenue_growth_deceleration_quarters: int = Field(gt=0, default=2)
    gross_margin_decline_quarters: int = Field(gt=0, default=2)
    share_count_annual_growth_ceiling: float = Field(gt=0, default=0.15)
    cash_runway_floor_months: float = Field(gt=0, default=12.0)
    # K-3(自動化計画 2026-08-30):`screening.monitoring_metrics.
    # MonitoringThresholds.concentration_drop_pct_points` と同じ既定値。
    concentration_drop_pct_points: float = Field(gt=0, default=0.05)


class PositionsConfig(BaseModel):
    """`config/positions.yaml` 全体。ファイルが無い場合は `positions=[]` として
    扱う(load_positions_config が空リストを返す。保有が無い状態は正常)。"""

    positions: list[Position] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener"
    # 18.6:APIレイヤーは読み取り専用ロールを使う(scripts/create_readonly_role.sql)。
    # 未設定時は database_url にフォールバックする(開発初期段階の後方互換)。
    api_database_url: str | None = None
    # 30.3.1:SECが要求する連絡先つき User-Agent。
    # 例 "TENX personal research <your-address@example.com>"
    # 未設定のままEDGARバッチを動かすと ValueError で落ちる(規約違反を
    # 黙って犯さないため)。
    edgar_user_agent: str | None = None
    # 30.8.1:FRED APIキー。未設定ならフェーズ7(マクロ)全体を無効として扱う。
    fred_api_key: str | None = None
    # K-9:Anthropic APIキー。未設定ならLLM機能(要約・定性サブスコア・レポート)
    # 全体を無効として扱う——FRED と同じ方針で、他の機能はすべて動く。
    # SDKは環境変数 ANTHROPIC_API_KEY を自分でも読むが、**設定の入口を1か所に
    # 揃えるため**にここでも読む(未設定を「無効」として静かに扱うか、
    # 呼び出し時に落とすかを、SDKではなくアプリ側で決めたい)。
    anthropic_api_key: str | None = None
    # K-9(docs/ui_llm_provider_selection_2026-08-30.md):`llm.provider = openai_compat`
    # のときのキー。ChatGPT なら OpenAI のキー、NIM なら NVIDIA のキー、ローカル
    # (Ollama 等)なら任意のダミー文字列。未設定なら openai_compat 経路を無効扱いにする。
    openai_api_key: str | None = None


def _load(model: type[BaseModel], path: Path):
    """YAMLを読んで検証する。失敗は `ConfigSchemaError` に包む(28.17)。

    包むのは、呼び出し側が「設定の不一致」と「それ以外の検証エラー」を
    取り違えないようにするため。
    """
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigSchemaError(path, exc) from exc


def load_universe_config(path: Path | None = None) -> UniverseConfig:
    return _load(UniverseConfig, path or CONFIG_DIR / "universe.yaml")


def load_collection_config(path: Path | None = None) -> CollectionConfig:
    return _load(CollectionConfig, path or CONFIG_DIR / "collection.yaml")


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    return _load(ScoringConfig, path or CONFIG_DIR / "scoring.yaml")


def load_model_v5_config(path: Path | None = None) -> ModelV5Config:
    return _load(ModelV5Config, path or CONFIG_DIR / "model_v5.yaml")


def load_objectives_config(path: Path | None = None) -> ObjectivesConfig:
    return _load(ObjectivesConfig, path or CONFIG_DIR / "objectives.yaml")


def load_portfolio_config(path: Path | None = None) -> PortfolioConfig:
    return _load(PortfolioConfig, path or CONFIG_DIR / "portfolio.yaml")


def load_execution_config(path: Path | None = None) -> ExecutionConfig:
    """`config/execution.yaml` を読む(D-5 / I-7)。無ければ既定値を使う。"""
    target = path or CONFIG_DIR / "execution.yaml"
    if not target.exists():
        return ExecutionConfig()
    return _load(ExecutionConfig, target)


def load_edgar_config(path: Path | None = None) -> EdgarConfig:
    """`config/collection.yaml` の `edgar:` セクションを読む(30.3.1)。

    独立したファイルにしないのは、EDGARも「データ収集」という同じ関心事の
    一部だから——`collection.yaml` に既にある yfinance の設定と並べて置く。
    """
    with (path or CONFIG_DIR / "collection.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return EdgarConfig.model_validate(raw.get("edgar") or {})
    except ValidationError as exc:
        raise ConfigSchemaError(path or CONFIG_DIR / "collection.yaml", exc) from exc


def load_monitoring_config(path: Path | None = None) -> MonitoringConfig:
    """`config/monitoring.yaml` を読む。ファイルが無ければ既定閾値を使う。"""
    target = path or CONFIG_DIR / "monitoring.yaml"
    if not target.exists():
        return MonitoringConfig()
    return _load(MonitoringConfig, target)


def load_positions_config(path: Path | None = None) -> PositionsConfig:
    """`config/positions.yaml` を読む。ファイルが無ければ空の保有として扱う
    (30.7.1:保有が無い状態は正常であり、エラーにしない)。"""
    target = path or CONFIG_DIR / "positions.yaml"
    if not target.exists():
        return PositionsConfig(positions=[])
    return _load(PositionsConfig, target)


def load_fred_config(path: Path | None = None) -> FredConfig:
    """`config/collection.yaml` の `fred:` セクションを読む(30.8)。無ければ既定値(enabled=True)。"""
    with (path or CONFIG_DIR / "collection.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return FredConfig.model_validate(raw.get("fred") or {})
    except ValidationError as exc:
        raise ConfigSchemaError(path or CONFIG_DIR / "collection.yaml", exc) from exc


def load_llm_config(path: Path | None = None) -> LlmConfig:
    """`config/collection.yaml` の `llm:` セクションを読む(K-9)。無ければ既定値。

    EDGAR/FREDと同じく `collection.yaml` に同居させる——「外部APIから何をどれだけ
    取ってくるか」という同じ関心事であり、ファイルを分けると設定の全体像が
    見えなくなる。
    """
    with (path or CONFIG_DIR / "collection.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return LlmConfig.model_validate(raw.get("llm") or {})
    except ValidationError as exc:
        raise ConfigSchemaError(path or CONFIG_DIR / "collection.yaml", exc) from exc


def get_settings() -> Settings:
    return Settings()
