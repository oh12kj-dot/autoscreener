/**
 * 日次ジョブの工程(§3.5、docs/daily_job_status_screen_2026-08-30.md)の日本語名と、
 * 工程ごとの結果整形。`pipeline_stage_runs.result` を生JSONのまま出さず、
 * 「通過1,260 / 除外4,027」のように人間が読める形に直す(§6.3)。
 * 個別の整形を持たない工程・未知のキーはフォールバックで `key: value` 表示にする。
 */

export const STAGE_LABELS: Record<string, string> = {
  universe_refresh: "ユニバース再取得",
  cik_map_refresh: "CIK突合",
  macro: "マクロ系列収集",
  xbrl_facts: "XBRL実績値収集",
  events: "決算カレンダー収集",
  insider: "インサイダー取引収集",
  short_interest: "空売り残収集",
  collection: "データ収集",
  gates: "除外ゲート適用",
  backtest: "バックテスト(週次)",
  scoring: "スコアリング",
  forward_validation: "前方検証",
  filings: "提出書類収集",
  monitoring: "四半期モニタリング",
  backup: "バックアップ",
};

/** 週次工程かどうか(§3.5)。C節で既定折りたたみにする判定に使う。 */
export const WEEKLY_STAGES = new Set([
  "universe_refresh",
  "cik_map_refresh",
  "macro",
  "xbrl_facts",
  "events",
  "insider",
  "short_interest",
  "backtest",
]);

/** 8.4/18.4の「中核工程」(monitoring.CORE_STAGES と揃える)。 */
export const CORE_STAGES = new Set(["collection", "gates", "scoring", "forward_validation"]);

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

export function skipReasonLabel(reason: string | null): string {
  if (reason === "not_weekly") return "本日は対象外(週次工程)";
  return reason ?? "対象外";
}

function num(result: Record<string, unknown>, key: string): number {
  const v = result[key];
  return typeof v === "number" ? v : 0;
}

function fallbackFormat(result: Record<string, unknown>): string {
  const entries = Object.entries(result);
  if (entries.length === 0) return "処理対象なし";
  return entries
    .map(([k, v]) => `${k}: ${typeof v === "number" ? v.toLocaleString() : String(v)}`)
    .join(" / ");
}

/**
 * 工程1つの結果を、C節(工程一覧)の1行に出す文字列にする。
 * `succeeded` の行にのみ使う(`failed`/`skipped`/`running` は別の表示)。
 */
export function formatStageResult(stage: string, result: Record<string, unknown> | null): string {
  if (result == null) return "";

  switch (stage) {
    case "collection": {
      // quarantined/universe_size は隔離状態の付帯情報(§4.1)であり、
      // その日の収集件数には含めない。
      const { quarantined: _quarantined, universe_size: _universeSize, ...counts } = result;
      const total = Object.values(counts).reduce<number>((a, v) => a + (typeof v === "number" ? v : 0), 0);
      return `${total.toLocaleString()}件処理`;
    }
    case "gates": {
      const included = num(result, "included");
      const excluded =
        num(result, "excluded") + num(result, "no_data") + num(result, "delisted") + num(result, "benchmark");
      return `通過${included.toLocaleString()} / 除外${excluded.toLocaleString()}`;
    }
    case "scoring": {
      if (typeof result.skipped_reason === "string" && result.skipped_reason) {
        return `中断:${result.skipped_reason}`;
      }
      return `スコア付与${num(result, "scored").toLocaleString()}件`;
    }
    case "forward_validation":
      return `算出${num(result, "computed").toLocaleString()}件`;
    default:
      return fallbackFormat(result);
  }
}

/** 失敗行の要約(`reason` は例外クラス名)。 */
export function formatStageFailure(reason: string | null): string {
  return reason ?? "失敗";
}
