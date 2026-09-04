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
    # S-7(2026-09-04、docs/daily_pipeline_throughput_plan_2026-09-04.md):
    # S-2で財務諸表を週次(月曜)へ格下げした副作用として、決算が週の半ばに
    # 出ると`apply_gates`への反映が最大6日遅れる(実測:非月曜変化75/220件、
    # 平均4.8日遅延)。`event_calendar`の次回決算日を過ぎたティッカーは、
    # 週次を待たずに財務諸表を再取得する。この値は「決算日から何日後まで
    # 再取得を試み続けるか」の猶予日数——yfinanceのfundamentals-timeseriesは
    # 決算発表の反映に1〜2日ラグがあるため、決算日当日1回だけでは
    # 発表前の古い値を掴んで終わってしまう(`snapshot_collector.py`の
    # `_earnings_triggered_refetch`docstring参照)。0にすると決算日当日のみ。
    statement_refresh_grace_days: int = Field(ge=0, default=3)
    shares_refresh_interval_days: int = Field(gt=0, default=7)
    market_session_min_coverage: float = Field(gt=0, le=1, default=0.90)
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
    """Phase 1 reliability contract; confidence never shifts the mean.

    WP-D (docs/racr_wp_d_reliability_layer_2026-09-04.md): ``ready_input_confidence``
    was, until this WP, used directly as a universe-wide flat constant --
    the root cause of ``model_confidence`` being exactly ``0.5`` for every
    scored ticker (docs/racr_shadow_run_diagnostic_2026-09-04.md §3.2). It
    is kept only as the fallback ``reliability.base_confidence_for`` returns
    when a real per-ticker evidence reliability cannot be computed at all.
    The real per-ticker value is
    ``min_base_confidence + (max_base_confidence - min_base_confidence) *
    core_evidence_reliability`` (``scoring/v5/reliability.py``).
    """

    ready_input_confidence: float = Field(ge=0, le=1, default=0.5)
    unavailable_input_confidence: float = Field(ge=0, le=1, default=0.0)
    min_base_confidence: float = Field(ge=0, le=1, default=0.10)
    max_base_confidence: float = Field(ge=0, le=1, default=0.90)
    # Half-life for the reporting-lag freshness decay applied to the core
    # (always-present) financial-statement evidence -- audit §7.3's
    # ``freshness(age) = exp(-ln2 * age / halfLife)``, ``age`` measured from
    # the latest annual statement's period-end date (WP-D trap 1:
    # deliberately NOT ``available_from``, which is a near-universal
    # constant -- see reliability.py's module docstring).
    statement_freshness_half_life_days: float = Field(gt=0, default=270.0)
    # q_sample targets: a "fully adequate" annual-statement history and
    # price-history length. ~756 trading days is ~3 calendar years,
    # matching this repository's typical daily-collection backfill depth.
    target_annual_periods: float = Field(gt=0, default=4.0)
    target_price_history_rows: float = Field(gt=0, default=756.0)

    @model_validator(mode="after")
    def base_confidence_bounds_ordered(self) -> ModelV5ReliabilityConfig:
        if self.min_base_confidence >= self.max_base_confidence:
            raise ValueError("min_base_confidence must be < max_base_confidence")
        return self


class ModelV5PathRiskConfig(BaseModel):
    """WP-F1 (docs/racr_wp_f1_path_risk_2026-09-04.md): tuning for
    ``scoring/v5/path_risk.py``'s block-bootstrap historical-simulation
    estimator. Every field here only controls *how the simulation is run*
    (sample size, block length) -- none of them is a return/volatility
    parameter fit to any model output, and none of them is read from the
    V4 seed. Kept as its own config section (not folded into
    ``ModelV5ReliabilityConfig``) because it governs a simulation, not a
    reliability/confidence weight.
    """

    simulations: int = Field(gt=0, default=300)
    block_weeks: int = Field(gt=0, default=4)


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


class ModelV5QualityConfig(BaseModel):
    """Phase 4 quality/accounting/reinvestment parameters (GitHub Issue #3 §6).

    Every number here is a challenger parameter that bounds a state or an
    uncertainty adjustment; none is an additive rank score. Accounting
    quality widens sigma/left-tail only -- it must never lower a conditional
    mean (Issue §6.3), and the multipliers/penalties below are all upper
    bounds enforced in ``quality.py``, not direct point deductions.
    """

    # NOPAT proxy tax rate (operating_income * (1 - rate)); this is the
    # inverse of the 0.79 factor already hardcoded at
    # routes.py:3539-3540/3548 for the same proxy definition.
    nopat_tax_rate: float = Field(ge=0, le=1, default=0.21)
    min_annual_periods: int = Field(ge=2, default=2)
    # Periods further apart than this are not trusted for a CAGR/delta
    # extrapolation (restated or irregular fiscal-year gaps).
    max_measurement_years: float = Field(gt=0, default=6.0)
    min_measurement_days: int = Field(gt=0, default=300)

    # incremental_roic -> growth.duration_years (duration_multiplier <= 1).
    # Only shortens duration, and only when both growth is positive and
    # incremental ROIC sits below the hurdle rate; never extends duration.
    incremental_roic_hurdle_rate: float = Field(default=0.10)
    incremental_roic_weight: float = Field(ge=0, default=0.15)
    max_duration_reduction_years: float = Field(gt=0, default=2.0)

    # per_share_economics -> growth mean multiplier (mean_multiplier <= 1).
    per_share_gap_weight: float = Field(ge=0, le=1, default=0.25)
    max_mean_multiplier_reduction: float = Field(ge=0, lt=1, default=0.30)

    # cash_conversion -> economics.cash_conversion / reinvestment_efficiency
    # state values only; no distribution multiplier.
    cash_conversion_ni_floor_ratio: float = Field(gt=0, default=0.01)
    cash_conversion_ratio_winsor_abs: float = Field(gt=0, default=5.0)

    # accounting_quality -> uncertainty only (sigma_multiplier >= 1,
    # left_tail_extra >= 0). Never changes the conditional mean.
    accounting_sigma_max_multiplier: float = Field(ge=1, default=1.5)
    accounting_left_tail_extra_max: float = Field(ge=0, default=0.35)

    # reconciliation_confidence -> uncertainty.model_confidence only.
    reconciliation_confidence_penalty: float = Field(ge=0, le=1, default=0.10)

    ablation_enabled: bool = True

    @model_validator(mode="after")
    def quality_bounds_ordered(self) -> ModelV5QualityConfig:
        if self.min_measurement_days >= self.max_measurement_years * 365.25:
            raise ValueError("min_measurement_days must be < max_measurement_years in days")
        return self


class ModelV5CapitalConfig(BaseModel):
    """Phase 5 debt-maturity/liquidity/capital-allocation parameters (Issue #3 §7/§8/§12).

    Every parameter here bounds a multiplicative *reduction* applied to
    ``survival_probability`` -- none can ever raise it above the v4-seeded
    baseline. Good liquidity/no near-term maturity wall/no aggressive
    capital return gets ``1.0`` (no bonus), matching the same
    never-reward-merely-having-data convention as growth.py/quality.py.
    """

    # debt_maturity: principal due within 12 months (routes.py:3619's exact
    # definition, reused) vs cash + revolver_available.
    debt_maturity_weight: float = Field(ge=0, default=0.25)
    debt_maturity_min_survival_multiplier: float = Field(gt=0, le=1, default=0.85)

    # liquidity: cash runway from the latest annual FCF burn (only when FCF
    # is negative; a profitable/FCF-positive company gets no bonus either).
    liquidity_runway_floor_months: float = Field(gt=0, default=12.0)
    liquidity_weight: float = Field(ge=0, default=0.25)
    liquidity_min_survival_multiplier: float = Field(gt=0, le=1, default=0.85)

    # capital_allocation: trailing-window committed cash return (buyback +
    # dividend) net of raised capital (debt_raise + equity_raise), relative
    # to the cash balance. Issue §7: this reads only already-announced
    # events in a bounded trailing window -- it never extrapolates a
    # historical buyback rate forward.
    capital_allocation_lookback_days: int = Field(gt=0, default=365)
    capital_allocation_weight: float = Field(ge=0, default=0.20)
    capital_allocation_min_survival_multiplier: float = Field(gt=0, le=1, default=0.90)

    # future_dilution_capacity (2026-09-03, Phase 6, Issue #3 section 12):
    # ATM/shelf remaining authorization + unexercised options/warrants +
    # a variable-conversion flag from `dilution_capacity`. Connects to
    # growth's mean multiplier (future diluted share count -> per-share
    # value), NOT survival. Each dollar/share-count ratio component is
    # capped before weighting so one outlier filing cannot dominate.
    future_dilution_atm_shelf_component_cap: float = Field(ge=0, default=0.50)
    future_dilution_options_component_cap: float = Field(ge=0, default=0.50)
    future_dilution_variable_conversion_bump: float = Field(ge=0, default=0.05)
    future_dilution_weight: float = Field(ge=0, default=0.15)
    future_dilution_max_reduction: float = Field(ge=0, lt=1, default=0.15)
    # Explicit anti-triple-counting ceiling (user-decided 2026-09-03): the
    # *combined* mean-multiplier reduction from Phase 4's per_share_economics
    # (realized, historical per-share-vs-whole-company CAGR gap) and this
    # signal (unissued, forward-looking capacity) is capped here, with this
    # signal's own contribution explicitly reduced by whatever budget
    # per_share_economics/incremental_roic already consumed -- not just an
    # independent second cap stacked on top (docs/model_v5_phase6_tail_
    # macro_competing_risk_2026-09-03.md "Triple-counting" section).
    max_combined_dilution_reduction: float = Field(ge=0, lt=1, default=0.35)

    ablation_enabled: bool = True

    @model_validator(mode="after")
    def capital_bounds_ordered(self) -> ModelV5CapitalConfig:
        floors = (
            self.debt_maturity_min_survival_multiplier,
            self.liquidity_min_survival_multiplier,
            self.capital_allocation_min_survival_multiplier,
        )
        if any(floor <= 0 or floor > 1 for floor in floors):
            raise ValueError("survival multiplier floors must lie in (0, 1]")
        if self.future_dilution_max_reduction > self.max_combined_dilution_reduction:
            raise ValueError(
                "future_dilution_max_reduction must be <= max_combined_dilution_reduction"
            )
        return self


class ModelV5TailConfig(BaseModel):
    """Phase 6 tail-risk parameters (Issue #3 section 12): customer
    concentration, litigation, and macro-regime downside exposure.

    Every parameter here bounds an additive ``left_tail_extra`` contribution
    (``>= 0``) -- none can ever lower the conditional mean, matching Phase 4
    accounting_quality's contract. M&A competing risk has no config here: it
    is not implemented at all (see tail_risk.py's module docstring).
    """

    customer_concentration_weight: float = Field(ge=0, default=0.20)
    customer_concentration_left_tail_max: float = Field(ge=0, default=0.20)

    litigation_lookback_days: int = Field(gt=0, default=365)
    # No severity/amount field exists on litigation_events yet; event count
    # within the lookback window is the proxy, capped at this count.
    litigation_severity_count_cap: int = Field(gt=0, default=3)
    litigation_weight: float = Field(ge=0, default=0.15)
    litigation_left_tail_max: float = Field(ge=0, default=0.15)

    macro_regime_weight: float = Field(ge=0, default=0.10)
    macro_regime_left_tail_max: float = Field(ge=0, default=0.10)

    # Shared ceiling across all three Phase 6 signals combined (not
    # coordinated with Phase 4 accounting_quality's own
    # accounting_left_tail_extra_max -- both are summed by engine.py and
    # this is Phase 6's own bound on its own contribution).
    max_combined_left_tail_extra: float = Field(ge=0, default=0.35)

    ablation_enabled: bool = True


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
    implementation_version: str = Field(default="v5.phase6", pattern=r"^v5\.phase\d+$")
    target_horizon_years: int = Field(gt=0, le=30, default=7)
    target_moic: float = Field(gt=1, default=10.0)
    reliability: ModelV5ReliabilityConfig = Field(default_factory=ModelV5ReliabilityConfig)
    path_risk: ModelV5PathRiskConfig = Field(default_factory=ModelV5PathRiskConfig)
    uncertainty: ModelV5UncertaintyConfig = Field(default_factory=ModelV5UncertaintyConfig)
    growth: ModelV5GrowthConfig = Field(default_factory=ModelV5GrowthConfig)
    quality: ModelV5QualityConfig = Field(default_factory=ModelV5QualityConfig)
    capital: ModelV5CapitalConfig = Field(default_factory=ModelV5CapitalConfig)
    tail: ModelV5TailConfig = Field(default_factory=ModelV5TailConfig)
    scenario_weights: ModelV5ScenarioWeights
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class ObjectiveDefinition(BaseModel):
    enabled: bool = True
    description: str
    downside_lambda: float | None = Field(default=None, ge=0)
    right_tail_moic: float | None = Field(default=None, gt=1)
    # Phase 10 (docs/model_v5_phase10_*.md): discount factors so a purely
    # reliability/quality-driven widening of the return distribution (which
    # mechanically raises any far-right-tail exceedance probability under
    # mean preservation -- a mathematical fact about the lognormal family,
    # not a bug in the distribution) does not also mechanically raise this
    # objective's rank. Read from `distribution["reliability_sigma_multiplier"]`
    # / `distribution["reliability_left_tail_extra"]` (Phase 10 additions to
    # the distribution contract), never from `model_confidence` -- confidence
    # stays reserved for missingness alone (Issue: "missingnessとconfidenceが
    # 分離"), not for how unreliable a *collected* signal says the company is.
    reliability_sigma_lambda: float | None = Field(default=None, ge=0)
    reliability_left_tail_lambda: float | None = Field(default=None, ge=0)
    # WP-B (docs/racr_wp_b_output_contract_2026-09-04.md; audit
    # autoscreener_racr_integrated_redesign_audit_2026-09-04.md §5.2): the
    # `risk_adjusted_compounding` (RACR) objective's four penalty
    # coefficients. These are fixed *investment-policy* priors, not fitted
    # model parameters -- the audit explicitly rules out choosing them by
    # backtest optimization ("policy parameter(lambda)をfitしない"). Two of
    # the four terms they multiply (drawdown, permanent loss) are always 0
    # today because the underlying statistics are `unavailable`
    # (competing-risk/path-simulation not implemented yet) -- see
    # `evaluate_objectives`'s `risk_adjusted_compounding` branch, which
    # records this explicitly via `explanation["omitted_terms"]` so the
    # score is never misread as "risk-adjusted for permanent loss/drawdown".
    tail_lambda: float | None = Field(default=None, ge=0)
    drawdown_lambda: float | None = Field(default=None, ge=0)
    permanent_loss_lambda: float | None = Field(default=None, ge=0)
    uncertainty_lambda: float | None = Field(default=None, ge=0)
    # WP-B2 (docs/racr_wp_b2_risk_terms_2026-09-04.md; diagnostic
    # docs/racr_shadow_run_diagnostic_2026-09-04.md §3.1/§5): a *fifth*
    # coefficient, multiplying ``P(failure) * (1 - assumed_recovery)`` --
    # the failure-atom frequency term separated out of what used to be a
    # single degenerate tail measure (see ``evaluate_objectives``'s
    # ``risk_adjusted_compounding`` branch for the full formula). This is
    # deliberately a distinct field from ``permanent_loss_lambda`` above,
    # even though the audit's policy table happens to assign both an
    # identical 0.20 prior: ``failure_lambda`` multiplies this model's own
    # failure atom (bankruptcy/non-recovering delisting, priced via
    # ``ce_cagr_failure_floor`` -- an *assumed* recovery rate, not a
    # cause-classified estimate), while ``permanent_loss_lambda`` remains
    # reserved for the future competing-risk/recovery-distribution model
    # (WP-F) that ``p_permanent_loss`` still reports as `None` for. Collapsing
    # the two into one field would let a reader mistake "the model already
    # prices failure frequency" for "permanent loss is now measured" --
    # exactly the conflation this WP exists to prevent.
    failure_lambda: float | None = Field(default=None, ge=0)
    # Marks an objective as superseded-but-kept-working (audit §0/§2.2:
    # `risk_adjusted` is replaced as the risk-aware objective of record by
    # `risk_adjusted_compounding`, but stays enabled and computed -- the
    # champion/challenger comparison in docs/model_v5_validation.md needs
    # its historical values). Never used to disable or hide an objective by
    # itself; `enabled` still controls that independently.
    deprecated: bool = False


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
    # S-5(2026-09-04、docs/daily_pipeline_throughput_plan_2026-09-04.md):
    # litigation/filing_sections/dilution/customer_concentrationの銘柄ループを
    # 並列化する際のワーカー数。実効レートの上限は共有`sec`リミッター
    # (`requests_per_second`)側で効くので、これを大きくしても実効レートは
    # 上がらない——1リクエストの往復時間(ネットワークレイテンシ)がリミッター
    # の間隔より長いと並列度1では間隔いっぱいに送信できず遊びが生じる。
    # その遊びを埋めるだけの値であり、レート上限を動かす設定ではない。
    max_workers: int = Field(gt=0, default=10)
    litigation_cik_batch_size: int = Field(gt=0, le=100, default=50)
    litigation_overlap_days: int = Field(ge=1, le=30, default=2)
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
