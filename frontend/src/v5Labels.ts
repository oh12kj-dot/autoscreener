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
  risk_adjusted: "リスク調整後期待年率",
  asymmetric: "非対称性(右裾/左裾)",
  capital_preservation: "資本保全(生存確率×低損失)",
};

export function v5ObjectiveLabel(key: string): string {
  return V5_OBJECTIVE_LABELS[key] ?? key;
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
