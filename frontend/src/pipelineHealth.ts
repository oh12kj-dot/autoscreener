/**
 * 日次ジョブの健全性所見(§3.4、daily_job_status_screen_2026-08-30.md)の
 * 日本語説明。
 *
 * バックエンドの `message` は既存3判定について**既存のログ文言をそのまま**
 * 使っている(英語のログ体裁のまま。`monitoring.py` 参照)。そのままUIに出すと
 * 利用者に読めないため、`code` ごとに独立した日本語文言をここで持つ
 * (§6.2。`warnings.ts` / `dueDiligence.ts` と同じ流儀)。
 */

export interface PipelineHealthInfo {
  label: string;
  describe: (detail: Record<string, unknown>) => string;
}

function num(detail: Record<string, unknown>, key: string): number | null {
  const v = detail[key];
  return typeof v === "number" ? v : null;
}

function str(detail: Record<string, unknown>, key: string): string | null {
  const v = detail[key];
  return typeof v === "string" ? v : null;
}

function pct(v: number | null, digits = 1): string {
  return v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
}

function count(v: number | null): string {
  return v == null ? "—" : v.toLocaleString();
}

export const PIPELINE_HEALTH_INFO: Record<string, PipelineHealthInfo> = {
  // 2026-08-29の主症状。全銘柄隔離でも収集対象選定は「例外なく完了」するため、
  // 既存の成功率判定(total=0で早期return)を素通りする。
  collection_target_empty: {
    label: "収集対象が0件",
    describe: (d) => {
      const universeSize = num(d, "universe_size");
      return universeSize != null
        ? `収集対象が0件でした(ユニバース${count(universeSize)}銘柄すべてが隔離中の可能性があります)。`
        : "収集対象が0件でした。";
    },
  },
  scoring_skipped: {
    label: "スコアリングが中断",
    describe: (d) => {
      const reason = str(d, "skipped_reason");
      return `スコアリングが中断し、ランキングが更新されていません。${reason ? `理由:${reason}` : ""}`;
    },
  },
  scoring_yield_dropped: {
    label: "スコア付与数が急減",
    describe: (d) =>
      `スコア付与数が前回実行から大きく減少しました(${count(num(d, "previous_scored"))}件 → ${count(num(d, "scored"))}件)。`,
  },
  collection_success_rate_low: {
    label: "収集成功率が低下",
    describe: (d) =>
      `収集成功率が${pct(num(d, "success_rate"))}まで低下しています(${count(num(d, "success"))}/${count(num(d, "total"))}件)。`,
  },
  sanitized_ratio_elevated: {
    label: "データ品質が劣化している可能性",
    describe: (d) =>
      `一部フィールドを無効化して採用したデータの比率が${pct(num(d, "sanitized_ratio"))}まで上昇しています(${count(num(d, "sanitized"))}/${count(num(d, "total"))}件)。`,
  },
  quarantine_ratio_high: {
    label: "隔離銘柄の比率が高い",
    describe: (d) =>
      `ユニバースのうち隔離中の銘柄が${pct(num(d, "ratio"))}(${count(num(d, "quarantined"))}/${count(num(d, "universe_size"))}件)に達しています。`,
  },
  stage_failed: {
    label: "工程が失敗",
    describe: (d) => `工程「${str(d, "stage") ?? "?"}」が失敗しました。`,
  },
  run_orphaned: {
    label: "実行が応答を停止",
    describe: () =>
      "プロセスが終了記録を残さないまま6時間以上経過しました。実行が異常終了した可能性があります。",
  },
};

export function pipelineHealthLabel(code: string): string {
  return PIPELINE_HEALTH_INFO[code]?.label ?? code;
}

export function pipelineHealthMessage(code: string, detail: Record<string, unknown>): string {
  const info = PIPELINE_HEALTH_INFO[code];
  if (!info) return "";
  try {
    return info.describe(detail);
  } catch {
    return "";
  }
}
