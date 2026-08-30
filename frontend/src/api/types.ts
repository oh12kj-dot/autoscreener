// バックエンド(autoscreener.api.schemas)のPydanticスキーマと対応する型定義。
//
// 27章でスコアリングが「8サブスコアの加重幾何平均(0〜100)」から
// 「P(7年で10倍)(0.0〜1.0)」に変わったため、スコア関連の型を入れ替えている。

/** 「何年で何倍」という目標と、そこから決まる必要年率(27.24)。 */
export interface TargetSpec {
  horizon_years: number;
  target_moic: number;
  /** target_moic^(1/horizon_years) - 1。年数と倍率を別々に見ると難易度を取り違えるため必ず併記する。 */
  required_cagr: number;
  is_default: boolean;
  /** 29章:この目標で有効な時価総額の上限(出口 $35B ÷ 目標倍率)。目標が緩いほど広がる。 */
  market_cap_ceiling: number;
  /** 同上、TTM売上高の上限(出口 $30B ÷ 目標倍率)。 */
  revenue_ceiling: number;
  /** 指定した目標が母集団の materialize 範囲(3倍)より緩く、上限が頭打ちになったか。 */
  universe_ceiling_capped: boolean;
}

export interface CandidateSummary {
  rank: number;
  ticker: string;
  company_name: string | null;
  sector: string | null;
  market_cap: number | null;
  price: number | null;
  /** P(7年で10倍)。0.0〜1.0。14.2のとおり通常は0.0001〜0.05のオーダー。 */
  probability: number;
  /** 各因子の中心的見通しを掛け合わせた点推定(=分布の平均) */
  expected_moic: number | null;
  /** 上を対数正規の平均とみなしたときの中央値 */
  median_moic: number | null;
  survival_probability: number | null;
  /**
   * 28.8:実測で較正済みの「バックテストのホライズンでオンペースに乗る確率」。
   * probability(7年で10倍)とは別の量で、**利用者が自分で答え合わせできる唯一の数字**。
   * 較正写像が無いとき(設定変更後にバックテスト未実行など)は null。
   */
  calibrated_on_pace_probability: number | null;
  /** C-1(model_audit_v4_2026-08-26.md):P(MOIC < 0.5)。上場廃止も損失として合成済み。 */
  probability_below_half: number | null;
  /** C-1:P(MOIC < 1.0)=元本割れ確率。 */
  probability_below_one: number | null;
  /** J-4:実現倍率の分位点(生存確率込みの混合分布)。幅を一覧でも見せるための任意フィールド。ソート対象ではない。 */
  moic_p10: number | null;
  moic_p90: number | null;
  /** C-4:クランプ到達・欠損データ・高レバレッジ等の警告コード一覧。 */
  warnings: string[];
  /** 30.2.1:証券口座で発注できるか。"tradable" / "not_listed" / "unknown"。 */
  tradability: string;
  tradable_brokers: string[];
  /** 30.2.2:20営業日平均売買代金。 */
  adv_usd: number | null;
  adv_observation_days: number | null;
  max_position_usd: number | null;
  /** "liquidity"(板が制約) / "portfolio"(規律が制約)。 */
  position_binding_constraint: string | null;
  /** 30.4.3:提出書類から読み取れる即死要因の件数(一覧では件数だけ)。 */
  blocking_flag_count: number;
  warning_flag_count: number;
  realized_vol_1y: number | null;
  max_drawdown_3y: number | null;
  evidence_grade: EvidenceGradeView | null;
}

/** 上位N銘柄をまとめて持ったときの見通し(28.12)。 */
export interface PortfolioOutlook {
  holdings: number;
  /** 共通因子の効き。0なら銘柄どうしは独立。 */
  asset_correlation: number;
  /** 期待本数 Σp。相関に依存しない。 */
  expected_hits: number;
  probability_at_least_one: number;
  /** 相関を無視した場合。独立性の錯覚がどれだけ楽観に振れるかを見せるために併記する。 */
  probability_at_least_one_if_independent: number;
  probability_at_least_two: number;
}

export interface CandidateListResponse {
  score_date: string | null;
  total: number;
  limit: number;
  offset: number;
  target: TargetSpec | null;
  items: CandidateSummary[];
  portfolio: PortfolioOutlook | null;
}

export interface ScoreHistoryPoint {
  /** J-3:その日の EV/粗利(scores.factors に日次で貯まる)。 */
  ev_to_gross_profit?: number | null;
  score_date: string;
  probability: number | null;
}

/** 15.1の恒等式に対応する因子。contribution は「MOICを何倍にしているか」(1.0が中立)。 */
export interface FactorBreakdown {
  key: string;
  label: string;
  value: number;
  contribution: number;
  explanation: string;
}

export interface CandidateDetail {
  ticker: string;
  company_name: string | null;
  is_candidate: boolean;
  sector: string | null;
  market_cap: number | null;
  price: number | null;
  probability: number | null;
  expected_moic: number | null;
  median_moic: number | null;
  log_moic_sigma: number | null;
  survival_probability: number | null;
  calibrated_on_pace_probability: number | null;
  /** C-1(model_audit_v4_2026-08-26.md):P(MOIC < 0.5)。上場廃止も損失として合成済み。 */
  probability_below_half: number | null;
  /** C-1:P(MOIC < 1.0)=元本割れ確率。 */
  probability_below_one: number | null;
  /**
   * J-4:実現倍率の分位点。キー "p10"/"p25"/"p50"/"p75"/"p90"。生存確率 1-S で ≈0、
   * S で対数正規、の混合分布から算出。**生の対数正規から出しており、実測では較正されていない**。
   * sigma_shrinkage により σ の銘柄差は 15% しか残らないため、幅はほぼ全銘柄で似た形になる。
   */
  moic_quantiles: Record<string, number> | null;
  /** C-4:クランプ到達・欠損データ・高レバレッジ等の警告コード一覧。 */
  warnings: string[];
  scoring_version: string | null;
  target: TargetSpec | null;
  /**
   * 27.20:数値の因子に加えて `unranked_reason`(文字列)が同居する。
   * 数値として扱う前に必ず typeof で確認すること。
   */
  factors: Record<string, number | string> | null;
  /** 順位が付かない理由。付いていれば "negative_outlook" など。 */
  unranked_reason: string | null;
  factor_breakdown: FactorBreakdown[];
  exclusion_reason: string[] | null;
  score_history: ScoreHistoryPoint[];
  last_updated: string | null;
  /** 30.2.1 / 30.2.2:取扱可否と流動性(CandidateSummaryと同じ意味)。 */
  tradability: string;
  tradable_brokers: string[];
  adv_usd: number | null;
  adv_observation_days: number | null;
  max_position_usd: number | null;
  adv_median_20d: number | null;
  adv_stress: number | null;
  zero_volume_days_60d: number;
  days_to_build: number | null;
  days_to_exit_stressed: number | null;
  position_binding_constraint: string | null;
  /** D-5:推定往復取引コスト(bps)。 */
  estimated_round_trip_cost_bps: number | null;
  /** 30.4.3:提出書類から読み取れる即死要因・注意事項。新しい順。 */
  red_flags: RedFlagView[];
  /** 追跡対象外でEDGARを一度も見ていない銘柄は null(空配列と区別する)。 */
  filings_checked_on: string | null;
  /** 30.6.2:将来の希薄化見通し。 */
  dilution_outlook: DilutionOutlook | null;
  /** 30.5:yfinance値とSEC XBRL値の突合。 */
  sec_reconciliation: ReconciliationItem[];
  /** J-1:会社概要(事業内容・IR・上場情報)。info 欠損の銘柄は null。 */
  profile: CompanyProfile | null;
  /** J-3:52週レンジと現在値の位置(0.0=安値〜1.0=高値)。値動きなしなら position は null。 */
  week52_high: number | null;
  week52_low: number | null;
  week52_position: number | null;
  /** J-6:直近のカタリスト(次回決算日 or ノートの検証日のうち近いほう)。無ければ null。 */
  next_event: CalendarEvent | null;
  /** J-7:需給(インサイダー・空売り残・浮動株)。表示専用(原則3)。 */
  supply: SupplyView | null;
  price_risk: PriceRiskView | null;
  evidence_grade: EvidenceGradeView | null;
  customer_concentration: CustomerConcentrationView[] | null;
  guidance: GuidanceView[] | null;
  litigation: LitigationView[] | null;
}

export interface PriceRiskView {
  observation_days: number; realized_vol_1y: number | null; max_drawdown_1y: number | null; max_drawdown_3y: number | null;
  max_drawdown_days_3y: number | null; recovery_days_3y: number | null; currently_in_drawdown: number | null;
  beta_1y: number | null; downside_capture_1y: number | null; benchmark_symbol: string | null;
}
export interface EvidenceGradeView { grade: string; reasons: string[]; clamp_count: number; missing_count: number; reconciliation_mismatch_count: number; period_count: number; }
export interface CustomerConcentrationView { period_end: string; customer_label: string; revenue_pct: number; }
export interface GuidanceView { filed_date: string; period_label: string; metric: string; low_usd: number | null; high_usd: number | null; }
export interface LitigationView { event_date: string; kind: string; title: string; detail: string | null; source_url: string | null; }

/** J-7(investment_decision_gap_2026-08-29.md):需給。ゲート・スコアには入らない。null は「未取得」で 0 とは別。 */
export interface SupplyView {
  insider_net_shares_180d: number | null;
  insider_buyer_count_180d: number | null;
  insider_as_of: string | null;
  short_interest_shares: number | null;
  days_to_cover: number | null;
  short_as_of: string | null;
  short_lag_days: number | null;
  public_float_usd: number | null;
  float_ratio: number | null;
}

/** J-1(investment_decision_gap_2026-08-29.md):会社の姿。原文のまま。要約・翻訳は生成しない。 */
export interface CompanyProfile {
  business_summary: string | null;
  website: string | null;
  industry: string | null;
  country: string | null;
  full_time_employees: number | null;
  exchange: string | null;
  listed_date: string | null;
  cik: string | null;
  /** raw_snapshots.snapshot_date。事業内容の記述がいつ時点のものか。 */
  profile_as_of: string | null;
  held_percent_insiders: number | null;
  held_percent_institutions: number | null;
  float_ratio: number | null;
  officers: OfficerView[];
}
export interface OfficerView { name: string; title: string | null; age: number | null; total_pay: number | null; }

/** 30.4.3:1件のレッドフラグ。 */
export interface RedFlagView {
  code: string;
  severity: "blocking" | "warning" | "info";
  detected_on: string;
  detail: string;
  document_url: string | null;
}

/** 30.6.2:1件の提出書類への参照(希薄化見通しの元ネタ)。 */
export interface FilingRef {
  accession_number: string;
  form: string;
  filed_date: string;
  document_url: string | null;
}

/** 30.6:将来の希薄化(モデルの株数外挿に入っていない予約済み分)。 */
export interface DilutionOutlook {
  shelf_filings: FilingRef[];
  offering_filings: FilingRef[];
  offerings_last_3y: number;
  historical_dilution_rate: number | null;
  /** 人間が research/<TICKER>.md に書いた値。未入力なら null(0や「なし」ではない)。 */
  remaining_shelf_capacity_usd: number | null;
  atm_remaining_usd: number | null;
  unexercised_options_ratio: number | null;
  has_variable_conversion_price: boolean | null;
  reserved_dilution_ratio: number | null;
}

/** J-2(investment_decision_gap_2026-08-29.md):1期(年次または四半期)の実績。取引通貨に統一済み。 */
export interface FinancialPeriodView {
  period_end: string;
  revenue: number | null;
  gross_profit: number | null;
  gross_margin: number | null;
  operating_income: number | null;
  net_income: number | null;
  operating_cash_flow: number | null;
  capex: number | null;
  free_cash_flow: number | null;
  cash_and_equivalents: number | null;
  total_debt: number | null;
  net_debt: number | null;
  shares_outstanding: number | null;
}

export interface PiotroskiCriterionView {
  key: string;
  label: string;
  met: boolean | null;
}

export interface FinancialHistoryDerivedView {
  revenue_yoy: number | null;
  revenue_cagr_3y: number | null;
  gross_margin_latest: number | null;
  /** 1四半期あたりの平均バーンレート(正の値=毎期これだけ現金が減る)。 */
  quarterly_burn_rate: number | null;
  /** 現金 ÷ 月次バーン。FCF が黒字なら null(実質無限)。 */
  runway_months: number | null;
  runway_floor_months: number | null;
  share_growth_rate: number | null;
  piotroski_score_ratio: number | null;
  piotroski_criteria_met: number;
  piotroski_criteria_computable: number;
  piotroski_criteria: PiotroskiCriterionView[];
}

export interface FinancialHistoryResponse {
  ticker: string;
  currency: string | null;
  currency_conversion_unavailable: boolean;
  annual: FinancialPeriodView[];
  quarterly: FinancialPeriodView[];
  derived: FinancialHistoryDerivedView;
  as_of: string | null;
  earnings: EarningsHistoryView | null;
}
export interface EarningsPeriodView { date: string; estimate: number | null; reported: number | null; surprise_pct: number | null; }
export interface EarningsHistoryView { covered: boolean; analyst_count: number | null; periods: EarningsPeriodView[]; beat_count_8q: number | null; miss_count_8q: number | null; mean_surprise_pct_8q: number | null; median_surprise_pct_8q: number | null; consecutive_beats: number | null; estimate_revision_30d: number | null; estimate_revision_7d: number | null; next_estimate: number | null; }

/** 30.5.3:1概念ぶんのyfinance値とSEC XBRL値の突合結果。 */
export interface ReconciliationItem {
  concept: string;
  model_value: number | null;
  sec_value: number | null;
  sec_tag: string | null;
  sec_period_end: string | null;
  sec_filed_date: string | null;
  relative_diff: number | null;
  status: "match" | "mismatch" | "magnitude_mismatch" | "unavailable";
}

export interface FilingListItem {
  accession_number: string;
  form: string;
  filed_date: string;
  report_date: string | null;
  items: string[];
  document_url: string | null;
}

export interface FilingListResponse {
  ticker: string;
  total: number;
  items: FilingListItem[];
}

/** J-10(investment_decision_gap_2026-08-29.md):円換算表示のための USD/JPY レート。表示用のみ。 */
export interface FxRateResponse {
  rate: number | null;
  as_of: string | null;
  source: string;
}

export interface MacroSeriesPoint {
  observation_date: string;
  value: number;
}

export interface MacroSeriesView {
  series_id: string;
  label: string;
  latest_value: number | null;
  latest_observation_date: string | null;
  change_3m: number | null;
  change_1y: number | null;
  history: MacroSeriesPoint[];
}

export interface MacroResponse {
  /** 30.8.4:FRED_API_KEY未設定でも200を返し、これで「未設定」を明示する。 */
  enabled: boolean;
  series: MacroSeriesView[];
}

export interface MonitoringMetricView {
  code: string;
  label: string;
  current_value: number | null;
  previous_value: number | null;
  triggered: boolean;
}

export interface AlertView {
  id: number;
  ticker: string;
  code: string;
  severity: string;
  source: string;
  triggered_on: string;
  detail: Record<string, unknown> | null;
  acknowledged_at: string | null;
}

export interface AlertsResponse {
  total: number;
  items: AlertView[];
}

export interface PositionView {
  ticker: string;
  opened_on: string;
  closed_on: string | null;
  shares: number;
  cost_basis_usd: number;
  binary_event: boolean;
  current_price: number | null;
  current_value_usd: number | null;
  unrealized_return: number | null;
  portfolio_weight: number | null;
  probability: number | null;
  monitoring_metrics: MonitoringMetricView[];
  open_alert_count: number;
  note_exists: boolean;
  note_is_complete: boolean;
  note_missing_fields: string[];
  /** J-8:達成倍率 = 現在値 ÷ 取得単価。 */
  achieved_moic: number | null;
  /** J-8:未到達で最小の trim_rule。閾値は売却シグナルではない(判断のやり直しの合図)。 */
  next_trim: NextTrim | null;
  /** J-8:点灯中の monitoring_metrics のうち exit_plan.thesis_break の indicator と一致したコード。 */
  thesis_break_hits: string[];
  thesis_evaluation_state: "none" | "unassessed" | "triggered";
  remaining_moic_to_target: number | null;
  remaining_years: number | null;
  required_cagr_from_here: number | null;
  required_cagr_at_entry: number | null;
}

/** J-8:次に到達する利食い計画の1段。 */
export interface NextTrim {
  at_moic: number;
  action: string | null;
  remaining_multiple: number | null;
}

/** 保有をまとめて持ったときの集計(30.7.5、元文書 第11節「相関と集中の管理」)。 */
export interface PortfolioSummary {
  total_cost_usd: number;
  position_count: number;
  sector_weights: Record<string, number>;
  sector_cap_breaches: string[];
  position_cap_breaches: string[];
  unprofitable_share: number | null;
}

export interface PositionsResponse {
  items: PositionView[];
  summary: PortfolioSummary;
  /** J-9:保有群をまとめて持ったときの見通し(相関込み)。保有0件では null。 */
  portfolio: PortfolioOutlook | null;
  /** J-9:現金比率(portfolio_value_usd − 取得原価合計)÷ portfolio_value_usd。 */
  cash_ratio: number | null;
  /** J-9:保有と現在のランキング上位の重複。 */
  ranking_overlap: string[];
  correlations: CorrelationView[];
}
export interface CorrelationView { a: string; b: string; correlation: number; overlap_days: number; }

export interface PeerView { ticker: string; company_name: string | null; market_cap: number | null; probability: number | null; rank: number | null; expected_moic: number | null; revenue_growth: number | null; gross_margin: number | null; ev_to_gross_profit: number | null; net_debt_to_gross_profit: number | null; share_growth_rate: number | null; }
export interface PeerResponse { ticker: string; peer_basis: string; peer_count: number; items: PeerView[]; }
export interface BenchmarkReferenceResponse { symbol: string; horizon_years: number; quantiles: Record<string, number> | null; }

export interface ResearchNoteResponse {
  ticker: string;
  exists: boolean;
  front_matter: Record<string, unknown>;
  body: string | null;
  missing_fields: string[];
  is_complete: boolean;
}

export interface UniverseStatusResponse {
  last_collection_run_at: string | null;
  universe_size: number;
  collection_status_counts: Record<string, number>;
  /**
   * B-6 / E-6(defect_audit_2026-08-27.md):実行中の途中経過を完了結果と
   * 区別するためのマーカー。`collection_target_count` が当日の対象件数、
   * `collection_complete` が true なら `collection_status_counts` の合計が最終値。
   * どちらも null のときはマーカー導入前の実行(進捗を判定できない)。
   */
  collection_target_count: number | null;
  collection_complete: boolean | null;
  gate_status_counts: Record<string, number>;
  scoring_status_counts: Record<string, number>;
}

/**
 * 日次ジョブ実行状況(14.15、daily_job_status_screen_2026-08-30.md §5)。
 * 「終了コード0」と「正常」を同一視しないための画面が使う。
 */
export interface PipelineHealthFinding {
  code: string;
  severity: "warning" | "error";
  message: string;
  detail: Record<string, unknown>;
}

export interface PipelineRunSummary {
  run_id: string;
  run_date: string;
  is_weekly: boolean;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  /** §3.3:3値ではなく4値。skipped/成功だがゼロ件/failedを混ぜない。 */
  status: "running" | "succeeded" | "degraded" | "failed";
  health: PipelineHealthFinding[];
  /** collected/gated_in/scored/quarantined/universe_size。値が無いキーはnull(0で埋めない)。 */
  headline: Record<string, number | null>;
  stage_summary: Record<string, number>;
}

export interface PipelineRunListResponse {
  runs: PipelineRunSummary[];
  /** これより前の実行は記録が無い(バックフィルしない。§2)。 */
  history_starts_at: string;
}

export interface PipelineStageView {
  stage: string;
  sequence: number;
  status: "running" | "succeeded" | "failed" | "skipped";
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  result: Record<string, unknown> | null;
  reason: string | null;
  error_message: string | null;
  error_traceback: string | null;
}

export interface PipelineRunDetail {
  /** `latest` を記録ゼロ件で呼んだときはnull(404にはしない)。 */
  run: PipelineRunSummary | null;
  stages: PipelineStageView[];
}

export interface ExcludedTicker {
  ticker: string;
  company_name: string | null;
  sector: string | null;
  exclusion_reason: string[];
}

export interface ExcludedListResponse {
  total: number;
  limit: number;
  offset: number;
  items: ExcludedTicker[];
}

export interface ScoreDatesResponse {
  dates: string[];
}

/** J-6(investment_decision_gap_2026-08-29.md):これから起きるイベント1件。アプリは日数だけを出す。 */
export interface CalendarEvent {
  ticker: string;
  company_name: string | null;
  event_type: "earnings" | "verification" | "manual";
  event_date: string;
  is_estimated: boolean;
  source: string;
  days_until: number;
  collected_on: string | null;
}

export interface CalendarResponse {
  as_of: string;
  items: CalendarEvent[];
}

export interface WatchlistEntry {
  ticker: string;
  company_name: string | null;
  sector: string | null;
  reason: string;
  reason_label: string;
  detail: string;
  gate: string | null;
}

export interface WatchlistResponse {
  snapshot_date: string | null;
  total: number;
  limit: number;
  offset: number;
  counts_by_reason: Record<string, number>;
  counts_by_gate: Record<string, number>;
  items: WatchlistEntry[];
}

export interface BacktestDecile {
  decile: number;
  count: number;
  mean_probability: number;
  median_return: number;
  on_pace_rate: number;
  loss_rate: number;
}

/** 評価日ごとのKPI(28.9)。平均だけを見ると検出力の低さが見えなくなる。 */
export interface BacktestPerDate {
  base_date: string;
  count: number;
  universe_on_pace_rate: number;
  top_decile_on_pace_rate: number;
  lift_ratio: number;
  rank_ic: number;
}

/**
 * 右裾の事象に対するリフト(28.11)。
 * 閾値は各評価日の断面リターン分位なので、強気相場でも弱気相場でも基準率は
 * quantile に固定される——残るのは「モデルが勝ち組を引けたか」だけ。
 */
export interface BacktestTailLift {
  quantile: number;
  median_threshold_return: number;
  top_decile_hit_rate: number;
  lift: number;
  worst_date_lift: number;
}

/** 較正曲線の1点(28.8)。予測がこの帯だった観測の実測頻度。 */
export interface BacktestCalibrationBin {
  lower: number;
  upper: number;
  count: number;
  mean_predicted: number;
  realized_rate: number;
}

/** 擬似バックテストの結果(27.8・14.2)。UIに常時出してモデルの検証状況を隠さない。 */
export interface BacktestSummary {
  run_at: string | null;
  scoring_version: string | null;
  horizon_years: number | null;
  observation_count: number;
  decile_monotonicity: number | null;
  lift_ratio: number | null;
  universe_on_pace_rate: number | null;
  top_decile_loss_rate: number | null;
  universe_loss_rate: number | null;
  calibration_error: number | null;
  delisted_settlement_rate: number | null;
  rank_ic: number | null;
  rank_ic_t_stat: number | null;
  lift_ratio_worst_date: number | null;
  /** S-8:価格ナウキャストが上限に張り付いている観測の割合。高いほど実質モメンタム加点化している。 */
  nowcast_cap_hit_rate: number | null;
  asset_correlation: number | null;
  is_calibrated: boolean;
  deciles: BacktestDecile[];
  per_date: BacktestPerDate[];
  tail_lifts: BacktestTailLift[];
  calibration_curve: BacktestCalibrationBin[];
  caveats: string[];
}

// ---------------------------------------------------------------------------
// K-9:LLM(Claude)による定性分析。**参考情報であり、ゲートにもスコアにも
// 入っていない**(サーバ側で `llm_analyses` 表に隔離されている)。
// 画面はこれを順位や除外の根拠のように見せてはならない。
// ---------------------------------------------------------------------------

/** トークン内訳。cache_read_tokens が常に0ならキャッシュが効いていない。 */
export interface LlmUsageView {
  input_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  output_tokens: number;
}

/** 要約の根拠になった提出書類。原文に戻るための情報だけを持つ。 */
export interface LlmSourceRef {
  accession_number: string | null;
  form: string | null;
  section: string | null;
  filed_date: string | null;
  source_url: string | null;
}

export interface LlmFilingSummaryView {
  source_key: string;
  as_of: string;
  model: string;
  effort: string;
  prompt_fingerprint: string;
  content: string;
  source_refs: LlmSourceRef[];
  usage: LlmUsageView | null;
  created_at: string | null;
}

export interface LlmQualitativeView {
  source_key: string;
  as_of: string;
  model: string;
  effort: string;
  prompt_fingerprint: string;
  business_summary: string | null;
  moat_evidence: string[];
  key_risks: string[];
  evidence_gaps: string[];
  /** low | medium | high の**順序尺度**。点数ではないので数値化して合成しないこと。 */
  conviction: string | null;
  conviction_rationale: string | null;
  source_refs: LlmSourceRef[];
  usage: LlmUsageView | null;
  created_at: string | null;
}

export interface LlmTickerAnalysisResponse {
  ticker: string;
  advisory: boolean;
  /** 画面に必ず出す断り書き。サーバが唯一の出典(画面ごとに書き分けない)。 */
  disclaimer: string;
  summaries: LlmFilingSummaryView[];
  qualitative: LlmQualitativeView | null;
}

export interface LlmReportResponse {
  advisory: boolean;
  disclaimer: string;
  /** 未生成は正常な状態(生成には課金が伴うので、作っていないのが既定)。 */
  exists: boolean;
  score_date: string | null;
  as_of: string | null;
  model: string | null;
  effort: string | null;
  content: string | null;
  ranked_symbols: string[];
  usage: LlmUsageView | null;
  created_at: string | null;
}

// K-9(ui_llm_provider_selection_2026-08-30.md):UIからのレポート生成。
// **これだけはAPIへの書き込み**で、課金が発生する。confirm を必須にしてある。

/** 選べるプロバイダ1つぶん。configured=false はAPIキー未設定で呼べない。 */
export interface LlmProviderInfo {
  provider: string;
  configured: boolean;
  default_model: string;
  suggested_models: string[];
  efforts: string[];
}

export interface LlmProvidersResponse {
  current: string;
  providers: LlmProviderInfo[];
}

export interface GenerateReportRequest {
  score_date?: string | null;
  top_n?: number;
  provider?: string | null;
  model?: string | null;
  effort?: string | null;
  /** 誤爆防止。false だと 400 が返る。UI は確認ダイアログの後で true を送る。 */
  confirm: boolean;
}

export interface GenerateReportResult {
  /** false は「同じ指紋の既存レポートがあった」。 */
  created: boolean;
  report: LlmReportResponse;
}

/** GET /llm/settings。collection.yaml / .env にアクティブな接続プロファイルを
 *  重ねた実効値。**APIキー本体は返らない** — set/未set のブール値だけ。 */
export interface LlmSettings {
  provider: string;
  base_url: string | null;
  model: string;
  effort: string;
  send_effort: boolean;
  anthropic_api_key_set: boolean;
  openai_api_key_set: boolean;
  active_connection_id: number | null;
  active_connection_name: string | null;
}

/** 保存済みの接続プロファイル1件。**api_key の本体は含まれない**(api_key_set のみ)。 */
export interface LlmConnection {
  id: number;
  name: string;
  provider: string;
  base_url: string | null;
  model: string | null;
  effort: string | null;
  send_effort: boolean;
  api_key_set: boolean;
  is_active: boolean;
}

export interface LlmConnectionsResponse {
  connections: LlmConnection[];
}

/** POST /llm/connections。name は一意。model/effort は空なら collection.yaml の既定。 */
export interface LlmConnectionCreate {
  name: string;
  provider: string;
  base_url?: string | null;
  model?: string | null;
  effort?: string | null;
  send_effort?: boolean;
  api_key?: string | null;
  activate?: boolean;
}

/** PUT /llm/connections/{id}。未指定は変更なし。base_url/model/effort に "" でクリア。
 *  api_key に "" で保存済みキーを削除。 */
export interface LlmConnectionUpdate {
  name?: string | null;
  provider?: string | null;
  base_url?: string | null;
  model?: string | null;
  effort?: string | null;
  send_effort?: boolean | null;
  api_key?: string | null;
}
