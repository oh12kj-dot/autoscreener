"""APIレスポンスのPydanticスキーマ(6.5)。"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

# 18.5:ティッカー文字列のフォーマット検証。
# クラス株のハイフン(BRK-B)に加えて、優先株等の "$"(FITB$I)とドット表記も許容する。
#
# **DBに存在する銘柄はすべて詳細を開けなければならない。** 以前は "$" を弾いて
# いたため、除外銘柄一覧に出ている24銘柄(NYSEの優先株)をクリックすると
# 422で何も表示されなかった——一覧に載っているのに開けない、という状態だった。
# ユニバース側でも優先株を除外するようにしたが(`universe_source`)、既存の
# `tickers` 行は次のユニバース再取得まで残るため、API側も受け付ける。
TICKER_PATTERN = r"^[A-Z0-9.$\-]{1,12}$"


class TargetSpec(BaseModel):
    """「何年で何倍」という目標の指定と、そこから決まる必要年率(27.24)。

    `required_cagr` を必ず一緒に返すのは、**年数と倍率を別々に見ていると
    難易度を取り違える**ため。「3年で3倍」(年率44.2%)は「7年で10倍」
    (年率38.9%)より厳しい、という関係は年率にしないと見えない。
    """

    horizon_years: float
    target_moic: float
    required_cagr: float
    is_default: bool
    # 29章:この目標で有効な規模の上限。目標が緩いほど緩む
    # (出口 $35B ÷ 目標倍率)。UIはこれを「この目標のユニバース」として表示する。
    market_cap_ceiling: float
    revenue_ceiling: float
    # 指定された目標が materialize 済みの母集団より緩く、上限が頭打ちになったか。
    # True のとき、上限は指定された目標ではなく `min_supported_target_moic`
    # (既定3.0倍)に対応する値になっている。
    universe_ceiling_capped: bool


class CandidateSummary(BaseModel):
    rank: int
    ticker: str
    company_name: str | None = None
    sector: str | None
    market_cap: float | None
    price: float | None
    # 27章:ランキングのキーは P(7年で10倍) そのもの。0.0〜1.0。
    probability: float
    # 各因子の中心的見通しを掛け合わせた点推定(=分布の平均)。
    # ホライズンを変えて再計算した場合は、その年数での値になる。
    expected_moic: float | None = None
    median_moic: float | None = None
    survival_probability: float | None = None
    # 28.8:実測で較正済みの「バックテストのホライズンでオンペースに乗る確率」。
    # `probability`(7年で10倍)とは別の量であり、**唯一利用者が自分で
    # 答え合わせできる数字**。較正写像が無いときは None。
    calibrated_on_pace_probability: float | None = None
    # C-1(2026-08-26、docs/model_audit_v4_2026-08-26.md):P(MOIC < 閾値)。
    # `probability`(右裾に届く確率)と対になる、下振れ側の確率。
    probability_below_half: float | None = None
    probability_below_one: float | None = None
    # J-4(docs/investment_decision_gap_2026-08-29.md):実現倍率の分位点(生存確率込みの
    # 混合分布)。幅を一覧でも見せるための任意フィールドで、**ソート対象にはしない**。
    moic_p10: float | None = None
    moic_p90: float | None = None
    # C-4:上位に偏っていたクランプ到達・欠損・高レバレッジ等を示す警告コード。
    # 意味は `/glossary` および `docs/model_audit_v4_2026-08-26.md` 参照。
    warnings: list[str] = []
    # 30.2.1:証券口座で発注できるか。"tradable" / "not_listed" / "unknown"。
    # リストファイルが無いときは全銘柄 "unknown"(不可と断定しない)。
    tradability: str = "unknown"
    tradable_brokers: list[str] = []
    # 30.2.2:20日平均売買代金と、そこから決まる1銘柄あたりの投入上限。
    adv_usd: float | None = None
    adv_observation_days: int | None = None
    max_position_usd: float | None = None
    adv_median_20d: float | None = None
    adv_stress: float | None = None
    zero_volume_days_60d: int = 0
    days_to_build: float | None = None
    days_to_exit_stressed: float | None = None
    # "liquidity"(板が制約) / "portfolio"(規律が制約)。どちらが効いているかを見せる
    position_binding_constraint: str | None = None
    # D-5(docs/defect_and_edge_audit_2026-08-28.md):推定往復取引コスト(bps)。
    # Corwin–Schultz 実効スプレッド + 平方根則マーケットインパクト。モデル確率が
    # 高くてもコストで食われる銘柄を順位表の上で識別できるようにする。
    estimated_round_trip_cost_bps: float | None = None
    # 30.4.3:提出書類から読み取れる即死要因の件数(一覧では件数だけ)。
    blocking_flag_count: int = 0
    warning_flag_count: int = 0
    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):このスコアが読んだデータの
    # 日付と、`score_date` からの営業日差。`data_age_days` が2営業日を超えたら
    # UIは「このランキングは古いデータで作られている」と明示する。
    price_as_of: datetime.date | None = None
    financials_as_of: datetime.date | None = None
    data_age_days: int | None = None
    realized_vol_1y: float | None = None
    max_drawdown_3y: float | None = None
    evidence_grade: "EvidenceGradeView | None" = None


class PortfolioOutlook(BaseModel):
    """上位N銘柄をまとめて持ったときの見通し(28.12)。

    銘柄ごとの確率を並べただけでは「20銘柄買えば1つは当たるだろう」という
    独立性の錯覚を招く。10バガーの発生は共通因子(マクロ・金利・セクター循環)に
    支配されており、実測でも評価日ごとのオンペース率は17%〜45%と振れている。
    """

    holdings: int
    asset_correlation: float
    expected_hits: float
    probability_at_least_one: float
    probability_at_least_one_if_independent: float
    probability_at_least_two: float


class EvidenceGradeView(BaseModel):
    grade: str
    clamp_count: int = 0
    missing_count: int = 0
    reconciliation_mismatch_count: int = 0
    period_count: int = 0
    reasons: list[str] = []


class PriceRiskView(BaseModel):
    observation_days: int
    realized_vol_1y: float | None = None
    max_drawdown_1y: float | None = None
    max_drawdown_3y: float | None = None
    max_drawdown_days_3y: int | None = None
    recovery_days_3y: int | None = None
    currently_in_drawdown: float | None = None
    beta_1y: float | None = None
    downside_capture_1y: float | None = None
    benchmark_symbol: str | None = None


class CandidateListResponse(BaseModel):
    score_date: datetime.date | None
    total: int
    limit: int
    offset: int
    target: TargetSpec | None = None
    items: list[CandidateSummary]
    # 28.12:表示中の上位銘柄をまとめて持った場合の見通し
    portfolio: PortfolioOutlook | None = None
    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):表示中の行のうち最も古い
    # データ齢(営業日)。2 を超えたらUIがページ全体に鮮度警告を出す。
    max_data_age_days: int | None = None


class ScoreHistoryPoint(BaseModel):
    score_date: datetime.date
    probability: float | None
    # J-3(docs/investment_decision_gap_2026-08-29.md):その日の EV/粗利。直近の
    # 織り込みの変化を見るために `probability` と併記する(`scores.factors` に
    # 日次で貯まっている値)。
    ev_to_gross_profit: float | None = None


class FactorBreakdown(BaseModel):
    """15.1の恒等式に対応する5因子分解と診断値(27章)。

    `expected_moic = revenue_multiple × margin_multiple × multiple_change
                     × leverage_effect ÷ dilution_drag`
    """

    key: str
    label: str
    value: float
    # 「この因子が単独でMOICを何倍にしているか」。1.0が中立。
    contribution: float
    explanation: str


class RedFlagView(BaseModel):
    """30.4.3:1件のレッドフラグ。"""

    code: str
    severity: str
    detected_on: datetime.date
    detail: str
    document_url: str | None = None


class FilingRef(BaseModel):
    """30.6.2:1件の提出書類への参照(希薄化見通しの元ネタ)。"""

    accession_number: str
    form: str
    filed_date: datetime.date
    document_url: str | None = None


class ReconciliationItemView(BaseModel):
    """30.5.3:1概念ぶんのyfinance値とSEC XBRL値の突合結果。"""

    concept: str
    model_value: float | None = None
    sec_value: float | None = None
    sec_tag: str | None = None
    sec_period_end: datetime.date | None = None
    sec_filed_date: datetime.date | None = None
    relative_diff: float | None = None
    status: str  # "match" / "mismatch" / "magnitude_mismatch" / "unavailable"


class DilutionOutlook(BaseModel):
    """30.6:将来の希薄化(モデルの株数外挿に入っていない予約済み分)。"""

    shelf_filings: list[FilingRef] = []
    offering_filings: list[FilingRef] = []
    offerings_last_3y: int = 0
    historical_dilution_rate: float | None = None
    remaining_shelf_capacity_usd: float | None = None
    atm_remaining_usd: float | None = None
    unexercised_options_ratio: float | None = None
    has_variable_conversion_price: bool | None = None
    reserved_dilution_ratio: float | None = None


class CalendarEvent(BaseModel):
    """J-6(docs/investment_decision_gap_2026-08-29.md):これから起きるイベント1件。

    アプリは日数だけを出す——「決算前に建てるな」とは書かない(それは判断)。
    """

    ticker: str
    company_name: str | None = None
    event_type: str  # 'earnings' / 'verification' / 'manual'
    event_date: datetime.date
    is_estimated: bool = False
    source: str
    days_until: int
    collected_on: datetime.date | None = None


class CalendarResponse(BaseModel):
    as_of: datetime.date
    items: list[CalendarEvent] = []


class SupplyView(BaseModel):
    """J-7(docs/investment_decision_gap_2026-08-29.md):需給(インサイダー・空売り残・浮動株)。

    **原則3:ゲート・スコアには一切入っていない。** 表示のみ。データが無い項目は
    None(「未取得」)であり 0 とは区別する。空売り残は遅延があるので
    `short_lag_days` を必ず一緒に出す。
    """

    insider_net_shares_180d: float | None = None
    insider_buyer_count_180d: int | None = None
    insider_as_of: datetime.date | None = None
    short_interest_shares: float | None = None
    days_to_cover: float | None = None
    short_as_of: datetime.date | None = None
    short_lag_days: int | None = None
    public_float_usd: float | None = None
    float_ratio: float | None = None


class CompanyProfile(BaseModel):
    """J-1(docs/investment_decision_gap_2026-08-29.md):会社の姿。

    `raw_snapshots.payload.info` に既にある一次情報を**原文のまま**出す。
    要約も翻訳も生成しない(原則1。生成要約は「読んだつもり」を作る)。
    `info` は日次で上書きされる二次情報なので**表示専用**——スコアリング・
    ゲートからは参照しない。
    """

    business_summary: str | None = None  # info.longBusinessSummary(原文のまま)
    website: str | None = None
    industry: str | None = None
    country: str | None = None
    full_time_employees: int | None = None
    exchange: str | None = None
    listed_date: datetime.date | None = None
    cik: str | None = None
    # 事業内容の記述がいつ時点のものか。古い可能性を隠さない。
    profile_as_of: datetime.date | None = None  # raw_snapshots.snapshot_date
    held_percent_insiders: float | None = None
    held_percent_institutions: float | None = None
    float_ratio: float | None = None
    officers: list["OfficerView"] = []


class OfficerView(BaseModel):
    name: str
    title: str | None = None
    age: int | None = None
    total_pay: float | None = None


class CandidateDetail(BaseModel):
    ticker: str
    company_name: str | None = None
    is_candidate: bool
    sector: str | None = None
    market_cap: float | None = None
    price: float | None = None
    probability: float | None = None
    expected_moic: float | None = None
    median_moic: float | None = None
    log_moic_sigma: float | None = None
    survival_probability: float | None = None
    calibrated_on_pace_probability: float | None = None
    # C-1(2026-08-26、docs/model_audit_v4_2026-08-26.md):P(MOIC < 閾値)。
    probability_below_half: float | None = None
    probability_below_one: float | None = None
    # J-4:実現倍率の分位点(P10/P25/P50/P75/P90)。生存確率 1-S で ≈0、S で対数正規、
    # の混合分布から算出。**生の対数正規から出す**(較正は閾値超過確率にしか
    # 掛かっていない単調写像なので分位点には適用できない)。入力欠損時は None。
    moic_quantiles: dict[str, float] | None = None
    # C-4:警告バッジのコード一覧。
    warnings: list[str] = []
    scoring_version: str | None = None
    # A-1(docs/defect_and_edge_audit_2026-08-28.md D-12):このスコアが読んだデータの日付。
    price_as_of: datetime.date | None = None
    financials_as_of: datetime.date | None = None
    data_age_days: int | None = None
    # 27.24:この詳細がどの目標(何年で何倍)で計算されたものか
    target: TargetSpec | None = None
    # 27.20:`unranked_reason` のような**文字列**のメタ情報が同じJSONに入る。
    # `dict[str, float]` にしていたため、見通しマイナスの銘柄(実データで256件)の
    # 詳細がレスポンス検証で失敗し、**全件500になっていた**。監視リストから
    # リンクを踏むと必ずエラーになる状態だった。
    factors: dict[str, float | str] | None = None
    # 順位が付かない理由。`factors` に埋もれていると型も意味も曖昧になるので、
    # APIの契約としては独立した項目にする(27.20が分けたかったのはまさにこれ)。
    unranked_reason: str | None = None
    factor_breakdown: list[FactorBreakdown] = []
    exclusion_reason: list[str] | None = None
    score_history: list[ScoreHistoryPoint] = []
    last_updated: datetime.datetime | None = None
    # 30.2.1 / 30.2.2:取扱可否と流動性(フェーズ1)。CandidateSummaryと同じ意味。
    tradability: str = "unknown"
    tradable_brokers: list[str] = []
    adv_usd: float | None = None
    adv_observation_days: int | None = None
    max_position_usd: float | None = None
    adv_median_20d: float | None = None
    adv_stress: float | None = None
    zero_volume_days_60d: int = 0
    days_to_build: float | None = None
    days_to_exit_stressed: float | None = None
    position_binding_constraint: str | None = None
    # D-5:推定往復取引コスト(bps)。
    estimated_round_trip_cost_bps: float | None = None
    # 30.4.3:提出書類から読み取れる即死要因・注意事項。新しい順。
    red_flags: list[RedFlagView] = []
    # 追跡対象外でEDGARを一度も見ていない銘柄は None(空リストと区別する。
    # 「調べて何も無かった」と「調べていない」を同じ表示にしてはならない)。
    filings_checked_on: datetime.date | None = None
    # 30.6.2:将来の希薄化見通し。追跡対象外の銘柄は None。
    dilution_outlook: DilutionOutlook | None = None
    # 30.5:yfinance値とSEC XBRL値の突合。XBRLデータが無い(追跡対象外/未収集)
    # 銘柄では空リスト。
    sec_reconciliation: list[ReconciliationItemView] = []
    # J-1:会社概要(事業内容・IR・上場情報)。`info` が欠損している銘柄は None
    # (185行が該当。例外にはしない)。
    profile: CompanyProfile | None = None
    # J-3:52週レンジと現在値の位置(0.0=安値〜1.0=高値)。`price_snapshots` から
    # 算出。価格履歴が無い銘柄は None。値動きが無い(高値=安値)と position は None。
    week52_high: float | None = None
    week52_low: float | None = None
    week52_position: float | None = None
    # J-6:直近のカタリスト(次回決算日 or ノートの検証日のうち近いほう)。無ければ None。
    next_event: CalendarEvent | None = None
    # J-7:需給(インサイダー・空売り残・浮動株)。表示専用(原則3)。
    supply: SupplyView | None = None
    price_risk: PriceRiskView | None = None
    evidence_grade: EvidenceGradeView | None = None
    customer_concentration: list["CustomerConcentrationView"] | None = None
    guidance: list["GuidanceView"] | None = None
    litigation: list["LitigationView"] | None = None


class CustomerConcentrationView(BaseModel):
    period_end: datetime.date
    customer_label: str
    revenue_pct: float


class GuidanceView(BaseModel):
    filed_date: datetime.date
    period_label: str
    metric: str
    low_usd: float | None = None
    high_usd: float | None = None


class LitigationView(BaseModel):
    event_date: datetime.date
    kind: str
    title: str
    detail: str | None = None
    source_url: str | None = None


class FinancialPeriodView(BaseModel):
    """J-2:1期(年次または四半期)の実績。取引通貨に統一済み。"""

    model_config = ConfigDict(from_attributes=True)

    period_end: datetime.date
    revenue: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    cash_and_equivalents: float | None = None
    total_debt: float | None = None
    net_debt: float | None = None
    shares_outstanding: float | None = None


class PiotroskiCriterionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    met: bool | None = None


class FinancialHistoryDerivedView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revenue_yoy: float | None = None
    revenue_cagr_3y: float | None = None
    gross_margin_latest: float | None = None
    # 1四半期あたりの平均バーンレート(正の値=毎期これだけ現金が減る)。
    quarterly_burn_rate: float | None = None
    # 現金 ÷ 月次バーン。FCF が黒字なら None(実質無限)。
    runway_months: float | None = None
    runway_floor_months: float | None = None
    share_growth_rate: float | None = None
    piotroski_score_ratio: float | None = None
    piotroski_criteria_met: int = 0
    piotroski_criteria_computable: int = 0
    piotroski_criteria: list[PiotroskiCriterionView] = []


class FinancialHistoryResponse(BaseModel):
    """J-2(docs/investment_decision_gap_2026-08-29.md):財務推移。**表示専用**であり
    `run-scoring` / `apply-gates` の出力には一切影響しない。"""

    ticker: str
    currency: str | None = None
    # 決算通貨 ≠ 取引通貨だが換算レートが取れなかった。系列は決算通貨のまま。
    currency_conversion_unavailable: bool = False
    annual: list[FinancialPeriodView] = []
    quarterly: list[FinancialPeriodView] = []
    derived: FinancialHistoryDerivedView
    as_of: datetime.date | None = None
    earnings: "EarningsHistoryView | None" = None


class EarningsPeriodView(BaseModel):
    date: str
    estimate: float | None = None
    reported: float | None = None
    surprise_pct: float | None = None


class EarningsHistoryView(BaseModel):
    covered: bool
    analyst_count: int | None = None
    periods: list[EarningsPeriodView] = []
    beat_count_8q: int | None = None
    miss_count_8q: int | None = None
    mean_surprise_pct_8q: float | None = None
    median_surprise_pct_8q: float | None = None
    consecutive_beats: int | None = None
    estimate_revision_30d: float | None = None
    estimate_revision_7d: float | None = None
    next_estimate: float | None = None


class UniverseStatusResponse(BaseModel):
    last_collection_run_at: datetime.datetime | None
    universe_size: int
    collection_status_counts: dict[str, int]
    # B-6(2026-08-26、docs/model_audit_v4_2026-08-26.md):実行中の途中経過を
    # 完了結果と区別するため。`collection_target_count` が対象件数、
    # `collection_complete` が真なら`collection_status_counts`の合計が最終値。
    collection_target_count: int | None = None
    collection_complete: bool | None = None
    gate_status_counts: dict[str, int]
    scoring_status_counts: dict[str, int]


class ExcludedTicker(BaseModel):
    ticker: str
    company_name: str | None = None
    sector: str | None
    exclusion_reason: list[str]


class ExcludedListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ExcludedTicker]


class ScoreDatesResponse(BaseModel):
    dates: list[datetime.date]


class WatchlistEntry(BaseModel):
    """Tier 2(監視対象、15.5)の1銘柄。"""

    ticker: str
    company_name: str | None = None
    sector: str | None
    reason: str
    reason_label: str
    detail: str
    gate: str | None = None


class WatchlistResponse(BaseModel):
    snapshot_date: datetime.date | None
    total: int
    limit: int
    offset: int
    counts_by_reason: dict[str, int]
    counts_by_gate: dict[str, int]
    items: list[WatchlistEntry]


class FilingListItem(BaseModel):
    """30.4.3: `GET /filings/{ticker}` の1件。"""

    accession_number: str
    form: str
    filed_date: datetime.date
    report_date: datetime.date | None = None
    items: list[str] = []
    document_url: str | None = None


class FilingListResponse(BaseModel):
    ticker: str
    total: int
    items: list[FilingListItem]


class MacroSeriesPoint(BaseModel):
    observation_date: datetime.date
    value: float


class MacroSeriesView(BaseModel):
    """30.8.2:1系列の現在値・変化・直近1年の推移。"""

    series_id: str
    label: str
    latest_value: float | None = None
    latest_observation_date: datetime.date | None = None
    change_3m: float | None = None
    change_1y: float | None = None
    history: list[MacroSeriesPoint] = []


class MacroResponse(BaseModel):
    # 30.8.4:FRED_API_KEY未設定でも200を返し、この値で「未設定」を明示する
    # (500にしない)。
    enabled: bool
    series: list[MacroSeriesView] = []


class FxRateResponse(BaseModel):
    """J-10:円換算表示のための USD/JPY レート。表示用の換算のみ。"""

    rate: float | None = None
    as_of: datetime.date | None = None
    source: str  # "fred:DEXJPUS" / "yfinance:JPY=X" / "unavailable"


class MonitoringMetricView(BaseModel):
    code: str
    label: str
    current_value: float | None = None
    previous_value: float | None = None
    triggered: bool


class AlertView(BaseModel):
    id: int
    ticker: str
    code: str
    severity: str
    source: str
    triggered_on: datetime.date
    detail: dict | None = None
    acknowledged_at: datetime.datetime | None = None


class AlertsResponse(BaseModel):
    total: int
    items: list[AlertView]


class NextTrim(BaseModel):
    """J-8:次に到達する利食い計画の1段。"""

    at_moic: float
    action: str | None = None
    # 現在の達成倍率からこの閾値までの倍率(at_moic − achieved_moic)。
    remaining_multiple: float | None = None


class PositionView(BaseModel):
    """30.7.5: `GET /positions` の1行。"""

    ticker: str
    opened_on: datetime.date
    closed_on: datetime.date | None = None
    shares: float
    cost_basis_usd: float
    binary_event: bool
    # 現在値(取得できたときのみ)
    current_price: float | None = None
    current_value_usd: float | None = None
    unrealized_return: float | None = None
    portfolio_weight: float | None = None
    # 最新スコア
    probability: float | None = None
    # 30.7.3:四半期モニタリング指標
    monitoring_metrics: list[MonitoringMetricView] = []
    # 未解消アラート件数
    open_alert_count: int = 0
    # 30.7.2:ノートの記入状況
    note_exists: bool = False
    note_is_complete: bool = False
    note_missing_fields: list[str] = []
    # J-8(docs/investment_decision_gap_2026-08-29.md):売却規律の達成度。
    # `achieved_moic` = 現在値 ÷ 取得単価。`next_trim` は未到達で最小の trim_rule。
    # `thesis_break_hits` は点灯中の monitoring_metrics のうち exit_plan.thesis_break の
    # indicator と一致したコード。**閾値は売却シグナルではない**(判断のやり直しの合図)。
    achieved_moic: float | None = None
    next_trim: NextTrim | None = None
    thesis_break_hits: list[str] = []
    thesis_evaluation_state: str = "unassessed"  # "none" / "unassessed" / "triggered"
    remaining_moic_to_target: float | None = None
    remaining_years: float | None = None
    required_cagr_from_here: float | None = None
    required_cagr_at_entry: float | None = None


class CorrelationView(BaseModel):
    a: str
    b: str
    correlation: float
    overlap_days: int


class PortfolioSummary(BaseModel):
    """保有をまとめて持ったときの集計(30.7.5、元文書 第11節「相関と集中の管理」)。"""

    total_cost_usd: float
    position_count: int
    sector_weights: dict[str, float] = {}
    sector_cap_breaches: list[str] = []  # config の sector_cap を超えたセクター
    position_cap_breaches: list[str] = []  # per_position_cap を超えた銘柄
    unprofitable_share: float | None = None  # 赤字銘柄の合計比率


class PositionsResponse(BaseModel):
    items: list[PositionView]
    summary: PortfolioSummary
    # J-9(docs/investment_decision_gap_2026-08-29.md):保有群をまとめて持ったときの見通し。
    # 保有0件では None。相関はランキング画面と同じ直近バックテスト由来。
    portfolio: PortfolioOutlook | None = None
    # 現金比率(portfolio_value_usd − 取得原価合計)÷ portfolio_value_usd。
    cash_ratio: float | None = None
    # 保有と現在のランキング上位の重複(同じテーゼに二重に賭けていないか)。
    ranking_overlap: list[str] = []
    correlations: list[CorrelationView] = []


class PeerView(BaseModel):
    ticker: str
    company_name: str | None = None
    market_cap: float | None = None
    probability: float | None = None
    rank: int | None = None
    expected_moic: float | None = None
    revenue_growth: float | None = None
    gross_margin: float | None = None
    ev_to_gross_profit: float | None = None
    net_debt_to_gross_profit: float | None = None
    share_growth_rate: float | None = None


class PeerResponse(BaseModel):
    ticker: str
    peer_basis: str
    peer_count: int
    items: list[PeerView] = []


class BenchmarkReferenceResponse(BaseModel):
    symbol: str
    horizon_years: float
    quantiles: dict[str, float] | None = None


class ResearchNoteResponse(BaseModel):
    ticker: str
    exists: bool
    front_matter: dict = {}
    body: str | None = None
    missing_fields: list[str] = []
    is_complete: bool = False


class BacktestDecile(BaseModel):
    decile: int
    count: int
    mean_probability: float
    median_return: float
    on_pace_rate: float
    loss_rate: float


class BacktestPerDate(BaseModel):
    """評価日ごとのKPI(28.9)。平均だけを見ると検出力の低さが見えなくなる。"""

    base_date: str
    count: int
    universe_on_pace_rate: float
    top_decile_on_pace_rate: float
    lift_ratio: float
    rank_ic: float


class BacktestTailLift(BaseModel):
    """右裾の事象に対するリフト(28.11)。閾値は各評価日の断面リターン分位。"""

    quantile: float
    median_threshold_return: float
    top_decile_hit_rate: float
    lift: float
    worst_date_lift: float


class BacktestCalibrationBin(BaseModel):
    """較正曲線の1点(28.8)。予測がこの帯だった観測の、実測頻度。"""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    realized_rate: float


class BacktestSummary(BaseModel):
    """直近の擬似バックテスト結果(27.8・14.2)。

    UIに常時出すのは、**モデルがまだ検証されていない**という事実を隠さない
    ためである。14.2は「上位デシルでも大半は外れる前提をUI上にも明示すること」
    を要件としている。
    """

    run_at: datetime.datetime | None = None
    scoring_version: str | None = None
    horizon_years: float | None = None
    observation_count: int = 0
    decile_monotonicity: float | None = None
    lift_ratio: float | None = None
    universe_on_pace_rate: float | None = None
    top_decile_loss_rate: float | None = None
    universe_loss_rate: float | None = None
    calibration_error: float | None = None
    delisted_settlement_rate: float | None = None
    delisted_count: int = 0
    delisted_settled_count: int = 0
    bankruptcy_count: int = 0
    mna_count: int = 0
    unknown_delisting_count: int = 0
    effective_independent_periods: float | None = None
    validation_status: str = "FAIL"
    validation_reasons: list[str] = []
    rank_ic: float | None = None
    rank_ic_t_stat: float | None = None
    lift_ratio_worst_date: float | None = None
    # S-8(2026-08-26、docs/model_audit_v4_2026-08-26.md):価格ナウキャストが上限に
    # 張り付いている観測の割合。高いほど「補正のはずが実質モメンタム加点に
    # なっている」ことを示す。
    nowcast_cap_hit_rate: float | None = None
    asset_correlation: float | None = None
    is_calibrated: bool = False
    # A-4/A-5(docs/defect_and_edge_audit_2026-08-28.md D-2/D-3):検出力とKPI合否。
    # `effective_dates` は Kish 実効評価日数、`*_ci` は評価日単位ブロック・
    # ブートストラップの95%CI。`non_overlapping` は保有期間の重ならない実行か。
    effective_dates: float | None = None
    non_overlapping: bool | None = None
    rank_ic_ci: list[float] | None = None
    lift_ratio_ci: list[float] | None = None
    decile_monotonicity_ci: list[float] | None = None
    # D-5:平均往復取引コスト(bps)と、コストを引いた後の主要KPI。
    mean_round_trip_cost_bps: float | None = None
    after_cost: dict | None = None
    # D-4:ポートフォリオ・シミュレーション(指数超過CAGR・最大ドローダウン等)。
    portfolio: dict | None = None
    # D-8:単純ベースライン(momentum_12m 等)の lift/monotonicity/rank_ic。
    baselines: dict | None = None
    # D-10:ライブ相当ゲート通過数 / 旧ゲート通過数。
    gate_parity: dict | None = None
    # D-3:14.2 成功指標の合否(PASS / FAIL / INSUFFICIENT_DATA)。
    kpi_verdicts: dict[str, str] = {}
    deciles: list[BacktestDecile] = []
    per_date: list[BacktestPerDate] = []
    tail_lifts: list[BacktestTailLift] = []
    calibration_curve: list[BacktestCalibrationBin] = []
    caveats: list[str] = []


# --- 日次ジョブ実行状況(14.15、docs/daily_job_status_screen_2026-08-30.md §5) --------


class PipelineHealthFinding(BaseModel):
    """健全性所見1件(§3.4)。`monitoring.HealthFinding` をそのまま写す。"""

    code: str
    severity: str  # "warning" / "error"
    message: str
    detail: dict


class PipelineRunSummary(BaseModel):
    """パイプライン1回分の実行概要(§5.1)。一覧・最新実行ヘッダの両方に使う。"""

    run_id: str
    run_date: datetime.date
    is_weekly: bool
    trigger: str
    started_at: datetime.datetime
    finished_at: datetime.datetime | None = None
    # §4.3:孤児実行(`finished_at` がNULLのまま6時間超)はAPI層で"failed"に
    # 差し替えて返す。DBの値そのものは書き換えない。
    duration_seconds: float | None = None
    status: str  # "running" / "succeeded" / "degraded" / "failed"
    health: list[PipelineHealthFinding] = []
    # 履歴ストリップで折れ線にする主要成果。工程の result から抽出(§5.1)。
    # 記録が無いキーはNone(0で埋めない。§2「やらないこと」と同じ判断)。
    headline: dict[str, int | None] = {}
    stage_summary: dict[str, int] = {}
    expected_stage_count: int


class PipelineRunListResponse(BaseModel):
    runs: list[PipelineRunSummary]
    # 本実装より前の実行は記録が無いことを画面が明示するための旗(§2)。
    history_starts_at: datetime.date


class PipelineStageView(BaseModel):
    """工程1つの詳細(§5.2)。"""

    stage: str
    sequence: int
    status: str  # "running" / "succeeded" / "failed" / "skipped"
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    duration_seconds: float | None = None
    result: dict | None = None
    reason: str | None = None
    error_message: str | None = None
    # 8000字で切ってある(§5.2)。個人利用・ローカル実行(11.1解釈A)であり、
    # APIは読み取り専用ロール(18.6)。運用者本人が読むためのものなので返す。
    error_traceback: str | None = None


class PipelineRunDetail(BaseModel):
    # `latest` を記録ゼロ件で呼んだときは None(404にはしない。§5.2)。
    run: PipelineRunSummary | None = None
    stages: list[PipelineStageView] = []


# ---------------------------------------------------------------------------
# K-9:LLM(Claude API)の定性分析。**参考値であり、ゲートにもスコアにも
# 入らない**(`src/autoscreener/llm/__init__.py`)。
#
# レスポンスに `advisory` と `disclaimer` を必ず載せるのは、UIがこれを
# スクリーニングの入力のように見せてしまう事故を防ぐため——表を分けたという
# サーバ側の保証は、JSONだけを見る利用者(自作のスクリプト等)には伝わらない。
# ---------------------------------------------------------------------------

# UIに出す固定文言。ここを唯一の出典にして、画面ごとに書き分けない。
LLM_DISCLAIMER = (
    "生成AI(Claude)による参考情報です。同じ入力でも出力は毎回変わりうるため、"
    "除外条件やランキングには一切使われていません。数値・結論は必ず原文で確認してください。"
)


class LlmUsageView(BaseModel):
    """トークン内訳。`cache_read_tokens` が常に0ならキャッシュが効いていない。"""

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0


class LlmSourceRef(BaseModel):
    """要約の根拠になった提出書類。原文に戻るための情報だけを持つ。"""

    accession_number: str | None = None
    form: str | None = None
    section: str | None = None
    filed_date: str | None = None
    source_url: str | None = None


class LlmFilingSummaryView(BaseModel):
    """`llm_analyses` の 1 行(kind='filing_summary')。"""

    source_key: str
    as_of: datetime.date
    model: str
    effort: str
    prompt_fingerprint: str
    content: str
    source_refs: list[LlmSourceRef] = []
    usage: LlmUsageView | None = None
    created_at: datetime.datetime | None = None


class LlmQualitativeView(BaseModel):
    """`llm_analyses` の 1 行(kind='qualitative')。

    `conviction` は low/medium/high の**順序尺度**であって点数ではない。
    数値化して他のスコアと合成しないこと(そうした瞬間、再現性の無い量が
    定量モデルに入る)。
    """

    source_key: str
    as_of: datetime.date
    model: str
    effort: str
    prompt_fingerprint: str
    business_summary: str | None = None
    moat_evidence: list[str] = []
    key_risks: list[str] = []
    evidence_gaps: list[str] = []
    conviction: str | None = None
    conviction_rationale: str | None = None
    source_refs: list[LlmSourceRef] = []
    usage: LlmUsageView | None = None
    created_at: datetime.datetime | None = None


class LlmTickerAnalysisResponse(BaseModel):
    """`GET /llm/{ticker}`。

    未生成の銘柄は404ではなく空で200を返す——`GET /research/{ticker}` と同じ
    立場で、「まだ作っていない」は正常な状態でありエラーではない
    (しかも生成には課金が伴うので、作っていないのが既定である)。
    """

    ticker: str
    advisory: bool = True
    disclaimer: str = LLM_DISCLAIMER
    summaries: list[LlmFilingSummaryView] = []
    qualitative: LlmQualitativeView | None = None


class LlmReportResponse(BaseModel):
    """`GET /llm/report`。当日ランキングの説明文(銘柄横断)。"""

    advisory: bool = True
    disclaimer: str = LLM_DISCLAIMER
    exists: bool = False
    # レポートが対象にした score_date(`source_key`)。
    score_date: datetime.date | None = None
    as_of: datetime.date | None = None
    model: str | None = None
    effort: str | None = None
    content: str | None = None
    ranked_symbols: list[str] = []
    usage: LlmUsageView | None = None
    created_at: datetime.datetime | None = None


# ---------------------------------------------------------------------------
# K-9(docs/ui_llm_provider_selection_2026-08-30.md):UIからのレポート生成。
#
# **`POST /llm/report/generate` はAPI層で唯一の書き込みであり、原則18.6を
# 意図的に破る。** 生成には課金が伴うので、`confirm=true` を必須にし、サーバ側で
# 短間隔のレート制限と同時実行ロックをかける(routes.py)。ブラウザのリロードや
# 監視ツールが1リクエストで請求を積み上げないようにするため。
# ---------------------------------------------------------------------------


class GenerateReportRequest(BaseModel):
    """`POST /llm/report/generate` の本文。"""

    score_date: datetime.date | None = None
    top_n: int = Field(default=10, ge=1, le=50)
    # None なら config/collection.yaml の既定を使う。
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    # 誤爆防止。明示の意思表示が無いリクエストは 400 で弾く。
    confirm: bool = False


class GenerateReportResult(BaseModel):
    """生成結果。`created=False` は「同じ指紋の既存レポートがあった」の意。"""

    created: bool
    report: LlmReportResponse


class LlmProviderInfo(BaseModel):
    """`GET /llm/providers` の1プロバイダぶん。UIのモデル選択がこれを読む。"""

    provider: str
    # APIキー等が揃っていて実際に呼べるか。
    configured: bool
    default_model: str
    suggested_models: list[str]
    efforts: list[str] = ["low", "medium", "high", "xhigh", "max"]


class LlmProvidersResponse(BaseModel):
    current: str
    providers: list[LlmProviderInfo]


class LlmSettingsResponse(BaseModel):
    """`GET /llm/settings`。いま実際に使われる LLM 接続の実効値(collection.yaml /
    .env にアクティブな接続プロファイルを重ねた結果)。

    **APIキー本体は絶対に返さない**——設定済みかどうかのブール値だけ。
    """

    provider: str
    base_url: str | None = None
    model: str
    effort: str
    send_effort: bool
    anthropic_api_key_set: bool
    openai_api_key_set: bool
    # アクティブなプロファイル(無ければ None = collection.yaml / .env のまま)。
    active_connection_id: int | None = None
    active_connection_name: str | None = None


class LlmConnectionView(BaseModel):
    """保存済みの接続プロファイル1件。**`api_key` の本体は含めない。**"""

    id: int
    name: str
    provider: str
    base_url: str | None = None
    model: str | None = None
    effort: str | None = None
    send_effort: bool = False
    api_key_set: bool = False
    is_active: bool = False


class LlmConnectionsResponse(BaseModel):
    connections: list[LlmConnectionView] = []


class LlmConnectionCreate(BaseModel):
    """`POST /llm/connections`。`name` は一意。`model` / `effort` は空なら
    collection.yaml の既定にフォールバックする。"""

    name: str
    provider: str = "anthropic"
    base_url: str | None = None
    model: str | None = None
    effort: str | None = None
    send_effort: bool = False
    api_key: str | None = None
    # true なら作成と同時にアクティブにする。
    activate: bool = False


class LlmConnectionUpdate(BaseModel):
    """`PUT /llm/connections/{id}`。

    `None` のフィールドは触らない。`base_url` / `model` / `effort` に `""` を
    渡すとその項目をクリア(→ collection.yaml の既定へ)。`api_key` に `""` で
    保存済みキーを削除する。
    """

    name: str | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    effort: str | None = None
    send_effort: bool | None = None
    api_key: str | None = None


# TENX v2 endpoints intentionally share metadata while keeping their payloads
# independent. This prevents CandidateDetail from becoming an unbounded schema.
class ReverseValuationScenarioView(BaseModel):
    required_return: float
    implied_revenue_cagr: float | None = None
    implied_terminal_margin: float | None = None
    implied_terminal_multiple: float | None = None
    feasible: bool
    reason: str | None = None
    tenx_gap: float | None = None
    consensus_gap: float | None = None
    guidance_gap: float | None = None


class InvestmentIntelligenceResponse(BaseModel):
    ticker: str
    as_of: datetime.date
    coverage_status: str
    source: str | None = None
    data_age_days: int | None = None
    not_used_in_ranking: bool = True
    data: dict | list | None = None


class ReverseValuationResponse(BaseModel):
    ticker: str
    as_of: datetime.date
    horizon_years: int
    coverage_status: str
    source: str = "tenx_core_assumptions"
    not_used_in_ranking: bool = True
    model_family: str
    model_supported: bool
    tenx_initial_growth: float | None = None
    consensus_growth: float | None = None
    management_guidance_growth: float | None = None
    scenarios: list[ReverseValuationScenarioView] = []
    return_distribution: dict[str, float] | None = None


class DataCoverageRow(BaseModel):
    dataset: str
    coverage: float
    stale: float
    failed: float
    last_successful: datetime.datetime | None = None
    source: str | None = None


class DataCoverageResponse(BaseModel):
    as_of: datetime.date
    ticker_count: int
    datasets: list[DataCoverageRow]
