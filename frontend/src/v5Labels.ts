/**
 * Model v5(shadow challenger)専用の内部識別子 -> 日本語ラベルの一元マッピング。
 *
 * Phase 11(2026-09-03の「v5のUIが見れたものではない」指摘への対応):
 * v5関連コンポーネントが snake_case の内部識別子(objective名・warningコード・
 * feature key・distribution status・decision値)をそのままユーザーに出していた。
 * v4の `warnings.ts`(WARNING_INFO + フォールバック関数)と同じ方針を踏襲する
 * ——1箇所に集約し、未知のキーは生の値へフォールバックする(新しいコードが
 * 増えても画面が壊れない)。内部識別子を完全に隠す必要はないので、ラベルの
 * 説明(title属性等)には原文を残せる箇所は残す。
 */

// -- objective(目的関数) -------------------------------------------------

export const V5_OBJECTIVE_LABELS: Record<string, string> = {
  ten_bagger: "10倍達成確率(P10x)",
  expected_return: "期待年率(CAGR)",
  risk_adjusted: "リスク調整後期待年率(旧式・非推奨)",
  // WP-C/WP-B (docs/racr_wp_c_api_ui_2026-09-04.md): RACR。shadow objective
  // であり default_objective ではない(config/objectives.yaml)。ラベルに
  // 「Shadow」を付けて、選択できても本番採用済みという意味ではないことを
  // 一覧のセレクタ上でも示す。
  risk_adjusted_compounding: "RACR(リスク調整後複利収益率・Shadow)",
  asymmetric: "非対称性(右裾/左裾)",
  capital_preservation: "資本保全(生存確率×低損失)",
};

export function v5ObjectiveLabel(key: string): string {
  return V5_OBJECTIVE_LABELS[key] ?? key;
}

/** RACRのexplanationに常に載る4つのペナルティ項のラベル。うち2項
 * (drawdown_lambda・permanent_loss_lambda)は`omitted_terms`により
 * 恒常的に0固定 —— ラベル自体にもその旨を含めておく。 */
export const V5_RACR_TERM_LABELS: Record<string, string> = {
  ce_cagr: "CE CAGR(確実性等価・複利年率)",
  // WP-B2(docs/racr_wp_b2_risk_terms_2026-09-04.md): v5.racr1の
  // tail_loss_10(下位10%を無条件に測る)は、生存確率が低い銘柄で全銘柄
  // 同一の定数(破綻atomのfloor)に潰れる欠陥があった。v5.racr2は生存条件
  // 付き(破綻atomを除いた継続部分だけ)で測る cond_tail_loss_10 に置換。
  // 旧キーはラベルごと残す(古いrunのexplanationにまだ残っているため)。
  tail_loss_10: "下位10%テール損失(年率換算・旧式)",
  cond_tail_loss_10: "下位10%テール損失(生存条件付き・年率換算)",
  // 「永久損失」ではない——現行モデル自身の破綻atom(倒産・非回収的上場
  // 廃止)の発生確率×保守的な回収率仮定。原因別competing-riskモデルに
  // よる推定である p_permanent_loss とは別物であることをラベルにも残す。
  failure_loss: "失敗頻度損失(P(失敗)×(1-回収率仮定)、永久損失ではない)",
  dd_excess: "ドローダウン超過(未実装のため常に0)",
  p_permanent_loss: "永久損失確率(未実装のため常に0)",
  model_uncertainty: "モデル不確実性(信頼度由来)",
  tail_lambda: "λ(テール)",
  failure_lambda: "λ(失敗頻度)",
  drawdown_lambda: "λ(ドローダウン)",
  permanent_loss_lambda: "λ(永久損失)",
  uncertainty_lambda: "λ(不確実性)",
};

export function v5RacrTermLabel(key: string): string {
  return V5_RACR_TERM_LABELS[key] ?? key;
}

// -- distribution.status の unavailable_reason(未実装メトリクス用) -------

export const V5_UNAVAILABLE_REASON_LABELS: Record<string, string> = {
  competing_risk_model_not_implemented:
    "破綻・上場廃止の原因別competing-riskモデルと回収率分布が未実装のため推定できません。",
  path_simulation_not_implemented:
    "保有期間中の価格経路シミュレーションが未実装のため推定できません(現行モデルは7年後の終端時点のみを扱います)。",
};

export function v5UnavailableReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "未実装のため推定できません。";
  return V5_UNAVAILABLE_REASON_LABELS[reason] ?? reason;
}

// -- 分布フィールドの列見出し・ラベル(Ranking/Detail共通) -----------------

export const V5_METRIC_LABELS: Record<string, string> = {
  ce_cagr: "CE CAGR",
  expected_cagr: "期待CAGR",
  median_cagr: "中央値CAGR",
  p_cagr_above_15: "P(CAGR>15%)",
  p_cagr_above_20: "P(CAGR>20%)",
  p_cagr_above_25: "P(CAGR>25%)",
  p_target: "上方余地 P(10x)",
  p_terminal_wealth_below_0_5: "大幅元本毀損確率(<0.5x)",
  p_permanent_loss: "永久損失確率",
  expected_max_drawdown: "予想最大ドローダウン",
  p_mdd_above_30: "P(MDD>30%)",
  p_mdd_above_50: "P(MDD>50%)",
  p_mdd_above_70: "P(MDD>70%)",
  recovery_time_median: "回復期間中央値",
  expected_shortfall_10pct_log: "下位10%期待損失(年率log)",
};

export function v5MetricLabel(key: string): string {
  return V5_METRIC_LABELS[key] ?? key;
}

// -- 数値の表示形式(監査 autoscreener_racr_integrated_redesign_audit_2026
// -09-04.md §6.3「小数点以下を過剰表示しない。CAGR/RACRは0.1pt、確率は
// 原則1pt、低確率だけ0.1ptまでとする」)。内部の12桁float(JSON経由でその
// まま来るPythonのfloat)を画面へ生で出さないための一元窓口。 -----------

/** CAGR・CE CAGR・RACRスコアなど「年率」系の値。0.1pt固定。 */
export function v5FormatRate(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

/** 確率系の値。1%以上は0.1pt、1%未満は「低確率」として0.01pt(0.1ptでは
 * P(10x)のような基準率1%未満の指標がすべて0.0%/0.1%に潰れてしまうため、
 * 既存v5画面(V5RankingSectionのpct())が採用していた粒度をそのまま踏襲)。 */
export function v5FormatProbability(v: number | null | undefined): string {
  if (v == null) return "—";
  const p = v * 100;
  if (p >= 1) return `${p.toFixed(1)}%`;
  if (p >= 0.01) return `${p.toFixed(2)}%`;
  if (p > 0) return "<0.01%";
  return "0.0%";
}

// -- distribution.status ---------------------------------------------------

export const V5_DISTRIBUTION_STATUS_LABELS: Record<string, string> = {
  available: "算出済み",
  unavailable: "算出不可",
  base_only: "基礎分布のみ",
};

export function v5DistributionStatusLabel(status: string): string {
  return V5_DISTRIBUTION_STATUS_LABELS[status] ?? status;
}

// -- ModelRun.status ---------------------------------------------------

export const V5_RUN_STATUS_LABELS: Record<string, string> = {
  succeeded: "完了",
  running: "実行中",
  failed: "失敗",
};

export function v5RunStatusLabel(status: string): string {
  return V5_RUN_STATUS_LABELS[status] ?? status;
}

// -- validation-status decision / mode ----------------------------------

export const V5_DECISION_LABELS: Record<string, string> = {
  CONTINUE_SHADOW: "シャドー運用を継続",
  PROMOTE_V5: "v5へ昇格",
  KEEP_V4: "v4を維持",
  UNDETERMINABLE: "判定不能(データ不足)",
};

export function v5DecisionLabel(decision: string): string {
  return V5_DECISION_LABELS[decision] ?? decision;
}

export const V5_MODE_LABELS: Record<string, string> = {
  shadow: "シャドー",
  active: "本番",
  legacy: "旧版",
};

export function v5ModeLabel(mode: string): string {
  return V5_MODE_LABELS[mode] ?? mode;
}

// -- signal / feature key(ablation行・coverage_gated_features等) --------

export const V5_SIGNAL_LABELS: Record<string, string> = {
  base_financial_statements: "基礎財務諸表",
  price_history: "株価履歴",
  tam_headroom: "TAMの残り余地",
  operating_kpi_nowcast: "業界KPIナウキャスト",
  consensus_revision: "アナリスト予想の修正",
  guidance: "経営陣ガイダンス",
  incremental_roic: "増分ROIC",
  per_share_economics: "一株当たり実質経済性",
  cash_conversion: "キャッシュ転換率",
  accounting_quality: "会計品質",
  reconciliation_confidence: "SEC原本との突合信頼度",
  capital_allocation: "資本配分実績",
  debt_maturity: "負債の満期構成",
  liquidity: "手元流動性",
  future_dilution_capacity: "将来の希薄化余力",
  customer_concentration: "顧客集中度",
  litigation: "訴訟リスク",
  macro_regime: "マクロ地合い(景気敏感度)",
  acquisition_competing_risk: "買収による上場廃止リスク",
};

export function v5SignalLabel(key: string): string {
  return V5_SIGNAL_LABELS[key] ?? key;
}

// -- state shift key(ablationの内訳:「〜 → 〜」表示の各行) --------------

export const V5_STATE_SHIFT_LABELS: Record<string, string> = {
  growth_duration_years: "成長期間",
  initial_growth_rate: "初期成長率",
  revenue_multiple_ratio: "売上マルチプル比率",
  sigma_multiplier: "分布の広がり(σ)倍率",
  left_tail_extra: "左テール追加リスク",
  survival_multiplier: "生存確率倍率",
  model_confidence: "モデル信頼度",
};

export function v5StateShiftLabel(key: string): string {
  return V5_STATE_SHIFT_LABELS[key] ?? key;
}

// -- ablationの status/reason ---------------------------------------------

export const V5_ABLATION_REASON_LABELS: Record<string, string> = {
  distribution_unavailable: "分布が算出不可のため未計算",
  disabled_by_config: "config で無効化されているため未計算",
  no_change_zero_growth_or_reduction: "成長率がゼロ以下で効果が出ないため計上なし",
  no_change_zero_overhang_or_budget_exhausted: "希薄化余地ゼロまたは予算超過のため計上なし",
  no_change_clamped_to_unity: "計算上の変化なし(クランプ済み)",
  objective_requires_later_phase_inputs: "後続フェーズの入力が必要なため未対応",
  runtime_disabled_low_coverage: "母集団全体のcoverageが閾値未満のため無効化",
  unavailable: "入力データなし",
  unsupported: "この目的関数では未対応",
  no_change: "変化なし",
  seed: "v4の初期値のまま(未更新)",
  candidate: "候補段階(未適用)",
  running: "計算中",
  ablated: "比較のためこの特徴量を除外中",
};

export function v5AblationReasonLabel(reason: string | undefined | null): string {
  if (!reason) return "理由不明(未記録)";
  return V5_ABLATION_REASON_LABELS[reason] ?? reason;
}

// -- warnings(run単位・銘柄単位・validation-status単位で共通) -----------

export interface V5WarningInfo {
  label: string;
  description: string;
}

const COVERAGE_GATED_PREFIX = "coverage_gated_features:";
const HISTORICAL_FORCED_OFF_PREFIX = "historical_mode: forced_off=";

export const V5_WARNING_INFO: Record<string, V5WarningInfo> = {
  not_for_production: {
    label: "投資判断には未使用",
    description:
      "v5はshadow challengerであり、実際の投資判断(v4のランキング)には一切使われていません。",
  },
  forward_shadow_only: {
    label: "将来検証のみ(過去再現は未対応)",
    description:
      "この特徴量はpoint-in-time整合性が確認できていないため、これから先に向けたシャドー実行でのみ有効です。過去日付での再現(historical backtest)では強制的に無効化されます。",
  },
  historical_backtest_supported_false: {
    label: "過去再現(historical backtest)は未対応",
    description:
      "この特徴量はhistorical_backtest_supported=falseのため、過去日付での再現時は強制的に無効化されます。",
  },
  phase6_state_updates_shadow_only: {
    label: "Phase6更新はシャドーのみ",
    description: "顧客集中・訴訟・マクロ地合いの状態更新はシャドー実行専用で、v4には一切反映されません。",
  },
  phase6_tail_macro_state_updates: {
    label: "テール/マクロ由来の状態更新を含む",
    description: "このrunには顧客集中・訴訟・マクロ地合いに由来する状態更新が含まれています。",
  },
  v4_champion_unchanged: {
    label: "v4への書き込みなし",
    description: "このrunはv4(Champion)のscoresテーブルを一切変更していません。",
  },
  financial_statement_pit_is_approximate: {
    label: "PIT財務諸表は近似値",
    description: "この時点で取得できる財務スナップショットは、開示タイミングの近似であり厳密なPITではありません。",
  },
  raw_snapshot_not_available_as_of: {
    label: "この時点の財務スナップショットなし",
    description: "as_of時点で参照できる財務スナップショットが取得されていません。",
  },
  distribution_unavailable: {
    label: "分布算出不可",
    description: "この銘柄の入力が不足しており、分布そのものを算出できていません。",
  },
  no_realized_outcome_backtest_available_for_either_model: {
    label: "両モデルとも実現アウトカムによる検証なし",
    description: "v4・v5のどちらも、実現リターンに基づくbacktestの評価対象日数が十分ではありません。",
  },
  forward_validation_zero_matured_observations: {
    label: "forward検証はまだ0件成熟",
    description:
      "評価対象期間がまだ短く、target_horizon_years(目標年数)に到達した銘柄がありません。0件は「効果なし」ではなく「まだ測れない」と読んでください。",
  },
};

// -- WP-C(docs/racr_wp_c_api_ui_2026-09-04.md):warningを4カテゴリへ分類し、
// バッジの色をカテゴリごとに変える(V5WarningBadges)。「鮮度の問題」「この
// モデルが構造的に対応していない」「母集団のcoverage不足」「まだ検証され
// ていない」は原因も対処法も違うため、同じ見た目の一律バッジに埋もれさせ
// ない。 --------------------------------------------------------------

export type V5WarningCategory = "stale" | "unsupported" | "low_coverage" | "unvalidated" | "other";

const V5_STALE_CODES = new Set([
  "raw_snapshot_not_available_as_of",
  "financial_statement_pit_is_approximate",
]);
const V5_UNSUPPORTED_CODES = new Set([
  "historical_backtest_supported_false",
  "forward_shadow_only",
  "phase6_state_updates_shadow_only",
]);
const V5_UNVALIDATED_CODES = new Set([
  "not_for_production",
  "no_realized_outcome_backtest_available_for_either_model",
  "forward_validation_zero_matured_observations",
]);

export function v5WarningCategory(code: string): V5WarningCategory {
  if (code.startsWith(COVERAGE_GATED_PREFIX)) return "low_coverage";
  if (code.startsWith(HISTORICAL_FORCED_OFF_PREFIX)) return "unsupported";
  if (V5_STALE_CODES.has(code)) return "stale";
  if (V5_UNSUPPORTED_CODES.has(code)) return "unsupported";
  if (V5_UNVALIDATED_CODES.has(code)) return "unvalidated";
  return "other";
}

export function v5WarningLabel(code: string): string {
  if (code.startsWith(COVERAGE_GATED_PREFIX)) {
    const keys = code.slice(COVERAGE_GATED_PREFIX.length).split(",").filter(Boolean);
    return `coverage不足で無効化された特徴量(${keys.length}件)`;
  }
  if (code.startsWith(HISTORICAL_FORCED_OFF_PREFIX)) {
    return "historical再現のため強制無効化された特徴量あり";
  }
  if (code === "historical_mode: no_features_forced_off") {
    return "historical再現(強制無効化なし)";
  }
  return V5_WARNING_INFO[code]?.label ?? code;
}

export function v5WarningDescription(code: string): string {
  if (code.startsWith(COVERAGE_GATED_PREFIX)) {
    const keys = code.slice(COVERAGE_GATED_PREFIX.length).split(",").filter(Boolean);
    const jaList = keys.map((k) => `${v5SignalLabel(k)}(${k})`).join("、");
    return `母集団全体のcoverageが閾値未満のため無効化されている特徴量: ${jaList}`;
  }
  if (code.startsWith(HISTORICAL_FORCED_OFF_PREFIX)) {
    const keys = code.slice(HISTORICAL_FORCED_OFF_PREFIX.length).split(",").filter(Boolean);
    const jaList = keys.map((k) => `${v5SignalLabel(k)}(${k})`).join("、");
    return `過去再現(historical backtest)のため強制無効化された特徴量: ${jaList}`;
  }
  return V5_WARNING_INFO[code]?.description ?? code;
}

// -- 一覧表の各行で毎回繰り返すと雑音になるboilerplate warning ------------
// (ページ上部の告知・run行で既に1回示されている内容と重複するため、
//  Phase 11で行レベルのバッジからは除外する対象。TickerDetail/Validation
//  など「1件だけ表示する」文脈ではそのまま出す——冗長にならないため。)
const V5_ROW_BOILERPLATE_WARNINGS = new Set([
  "not_for_production",
  "phase6_state_updates_shadow_only",
  "financial_statement_pit_is_approximate",
]);

export function v5FilterRowBoilerplateWarnings(codes: string[]): string[] {
  return codes.filter((code) => !V5_ROW_BOILERPLATE_WARNINGS.has(code));
}
