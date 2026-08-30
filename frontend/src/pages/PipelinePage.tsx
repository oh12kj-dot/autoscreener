import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchPipelineRun, fetchPipelineRuns, fetchScoreDates } from "../api/client";
import type { PipelineRunDetail, PipelineRunListResponse, PipelineStageView } from "../api/types";
import { pipelineHealthLabel, pipelineHealthMessage } from "../pipelineHealth";
import {
  formatStageFailure,
  formatStageResult,
  skipReasonLabel,
  stageLabel,
  WEEKLY_STAGES,
} from "../pipelineStages";

/**
 * 日次ジョブ実行状況画面(14.15、daily_job_status_screen_2026-08-30.md)。
 *
 * この画面が答えるべき問いは「ジョブは落ちたか」ではなく「**今表示されている
 * ランキングは、今日のデータで作られたものか**」である(§0)。工程が例外なく
 * 完走した上で成果が0件、という2026-08-29型の失敗こそが主対象であり、
 * 例外の表示はその副産物にすぎない。
 */

// §6.4:runningのときだけポーリングする。日次ジョブの状態は1日1回しか
// 変わらないので、それ以外は再取得しない。
const POLL_INTERVAL_MS = 15_000;

// §3.3:4値。色だけに意味を載せない(記号+語を必ず併記)。
const STATUS_META: Record<string, { symbol: string; label: string; className: string }> = {
  running: { symbol: "◐", label: "実行中", className: "pipeline-status-running" },
  succeeded: { symbol: "●", label: "正常", className: "pipeline-status-succeeded" },
  degraded: { symbol: "●", label: "要注意", className: "pipeline-status-degraded" },
  failed: { symbol: "●", label: "失敗", className: "pipeline-status-failed" },
};

// §3.4:健全性所見のcodeがどの工程に紐づくか。C節の「!」表示に使う
// (stage_failedは既にfailed行なので対象外——failedと!は重複させない)。
const HEALTH_CODE_STAGE: Record<string, string> = {
  collection_target_empty: "collection",
  collection_success_rate_low: "collection",
  sanitized_ratio_elevated: "collection",
  quarantine_ratio_high: "collection",
  scoring_skipped: "scoring",
  scoring_yield_dropped: "scoring",
};

function formatDateTimeJST(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const date = d.toLocaleDateString("ja-JP", { timeZone: "Asia/Tokyo" });
  const time = d.toLocaleTimeString("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit" });
  return `${date} ${time} JST`;
}

function formatRelative(iso: string | null): string {
  if (!iso) return "";
  const diffMs = Date.now() - new Date(iso).getTime();
  const hours = diffMs / (1000 * 60 * 60);
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}分前`;
  if (hours < 48) return `${Math.round(hours)}時間前`;
  return `${Math.round(hours / 24)}日前`;
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}秒`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}分${s}秒`;
}

function formatDelta(current: number | null | undefined, previous: number | null | undefined): string {
  if (current == null || previous == null) return "—";
  const diff = current - previous;
  if (diff === 0) return "変化なし";
  const symbol = diff > 0 ? "▲" : "▼";
  return `${symbol}${Math.abs(diff).toLocaleString()}`;
}

function quarantineRatio(headline: Record<string, number | null>): number | null {
  const q = headline.quarantined;
  const u = headline.universe_size;
  if (q == null || u == null || u === 0) return null;
  return q / u;
}

function pct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export function PipelinePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedRunId = searchParams.get("run");

  const [list, setList] = useState<PipelineRunListResponse | null>(null);
  const [detail, setDetail] = useState<PipelineRunDetail | null>(null);
  const [latestScoreDate, setLatestScoreDate] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [weeklyExpanded, setWeeklyExpanded] = useState(false);
  const [expandedStage, setExpandedStage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [listResp, detailResp, scoreDatesResp] = await Promise.all([
        fetchPipelineRuns(14),
        fetchPipelineRun(selectedRunId ?? "latest"),
        fetchScoreDates(1).catch(() => null),
      ]);
      setList(listResp);
      setDetail(detailResp);
      setLatestScoreDate(scoreDatesResp?.dates[0] ?? null);
      setError(null);
    } catch (e) {
      // §6.4:この画面自身がAPI障害を映す場でもあるため、白紙で終わらせない。
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selectedRunId]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // 月曜(is_weekly)の実行を見ているときは既定で週次工程を展開する(§6.3)。
  //
  // 依存配列を `run_id` だけに絞るのは意図的。`detail` を入れると、実行中の
  // 15秒ポーリング(§6.4)でオブジェクトの同一性が変わるたびにこの効果が再走し、
  // 利用者が読んでいる最中のトレースバックを勝手に畳んでしまう。「見ている実行が
  // 切り替わったとき」だけが展開状態をリセットしてよい契機である。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (detail?.run) setWeeklyExpanded(detail.run.is_weekly);
    setExpandedStage(null);
  }, [detail?.run?.run_id]);

  useEffect(() => {
    if (detail?.run?.status !== "running") return;
    const id = window.setInterval(() => {
      if (!document.hidden) load();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [detail?.run?.status, load]);

  const selectRun = (runId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("run", runId);
    setSearchParams(next);
  };

  if (loading && !detail) return <p>読み込み中...</p>;

  if (error) {
    return (
      <div>
        <h2>日次ジョブ</h2>
        <p className="error">実行状況を取得できませんでした(APIが停止している可能性があります)。{error}</p>
      </div>
    );
  }

  const run = detail?.run ?? null;
  const runs = list?.runs ?? [];
  const currentIndex = run ? runs.findIndex((r) => r.run_id === run.run_id) : -1;
  const previousRun = currentIndex >= 0 && currentIndex + 1 < runs.length ? runs[currentIndex + 1] : null;
  const stages = detail?.stages ?? [];
  const dailyStages = stages.filter((s) => !WEEKLY_STAGES.has(s.stage));
  const weeklyStages = stages.filter((s) => WEEKLY_STAGES.has(s.stage));
  const showAllInline = weeklyExpanded || weeklyStages.length === 0;
  const visibleStages = showAllInline ? stages : dailyStages;

  const flaggedStages = new Set(
    (run?.health ?? []).map((f) => HEALTH_CODE_STAGE[f.code]).filter((s): s is string => Boolean(s)),
  );

  const scoringStage = stages.find((s) => s.stage === "scoring");
  const scoringIsFresh =
    scoringStage?.status === "succeeded" &&
    !(scoringStage.result && typeof scoringStage.result === "object" && "skipped_reason" in scoringStage.result);

  const totalStagesCount = 15;
  const completedStagesCount = run
    ? (run.stage_summary.succeeded ?? 0) + (run.stage_summary.failed ?? 0) + (run.stage_summary.skipped ?? 0)
    : 0;

  return (
    <div>
      <h2>日次ジョブ</h2>
      <p className="ticker-meta">
        「終了コード0」は「正常」を意味しません。工程が例外なく完走した上で成果が0件になる、
        という失敗はここでしか見えません。平常時は見なくてよい画面です——ランキング画面の
        バナーが異常時にここへ誘導します。
      </p>

      {!run && (
        <p>
          記録された実行がまだありません。日次パイプライン(<code>tenx run-daily-pipeline</code>)を
          実行すると、ここに履歴が積み上がります。
        </p>
      )}

      {run && (
        <>
          {/* A. 最新実行サマリ */}
          <div className={`pipeline-summary ${STATUS_META[run.status]?.className ?? ""}`}>
            <div className="pipeline-summary-header">
              <span className="pipeline-status-badge">
                <span className="pipeline-status-symbol" aria-hidden="true">
                  {STATUS_META[run.status]?.symbol ?? "?"}
                </span>
                {STATUS_META[run.status]?.label ?? run.status}
              </span>
              <span className="pipeline-summary-time">
                {formatDateTimeJST(run.started_at)}({formatRelative(run.started_at)})
              </span>
              <span className="pipeline-summary-duration">所要 {formatDuration(run.duration_seconds)}</span>
            </div>

            {run.status === "running" ? (
              <p className="pipeline-running-note">
                実行中({completedStagesCount}/{totalStagesCount}工程完了)
              </p>
            ) : run.health.length > 0 ? (
              <ul className="pipeline-health-list">
                {run.health.map((f, i) => (
                  <li key={`${f.code}-${i}`} className={`pipeline-health-item pipeline-severity-${f.severity}`}>
                    <strong>{pipelineHealthLabel(f.code)}</strong>
                    <p>{pipelineHealthMessage(f.code, f.detail) || f.message}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="pipeline-ok-note">所見はありません。全工程が正常に完了しました。</p>
            )}

            {!scoringIsFresh && run.status !== "running" && (
              <p className="pipeline-stale-note">
                → 表示中のランキングは{latestScoreDate ? ` ${latestScoreDate} ` : "前回実行"}時点のものです
              </p>
            )}
          </div>

          {/* B. 成果の推移 */}
          <div className="pipeline-tiles">
            <div className="pipeline-tile">
              <div className="pipeline-tile-label">収集</div>
              <div className="pipeline-tile-value">{run.headline.collected?.toLocaleString() ?? "—"}</div>
              <div className="pipeline-tile-delta">
                {formatDelta(run.headline.collected, previousRun?.headline.collected)}
              </div>
            </div>
            <div className="pipeline-tile">
              <div className="pipeline-tile-label">ゲート通過</div>
              <div className="pipeline-tile-value">{run.headline.gated_in?.toLocaleString() ?? "—"}</div>
              <div className="pipeline-tile-delta">
                {formatDelta(run.headline.gated_in, previousRun?.headline.gated_in)}
              </div>
            </div>
            <div className="pipeline-tile">
              <div className="pipeline-tile-label">スコア付与</div>
              <div className="pipeline-tile-value">{run.headline.scored?.toLocaleString() ?? "—"}</div>
              <div className="pipeline-tile-delta">{formatDelta(run.headline.scored, previousRun?.headline.scored)}</div>
            </div>
            <div className="pipeline-tile">
              <div className="pipeline-tile-label">隔離率</div>
              <div className="pipeline-tile-value">{pct(quarantineRatio(run.headline))}</div>
              <div className="pipeline-tile-delta">
                {(() => {
                  const cur = quarantineRatio(run.headline);
                  const prev = previousRun ? quarantineRatio(previousRun.headline) : null;
                  if (cur == null || prev == null) return "—";
                  const diffPt = (cur - prev) * 100;
                  if (Math.abs(diffPt) < 0.05) return "変化なし";
                  return `${diffPt > 0 ? "▲" : "▼"}${Math.abs(diffPt).toFixed(1)}pt`;
                })()}
              </div>
            </div>
          </div>

          {/* C. 工程 */}
          <h3>工程</h3>
          <div className="pipeline-stage-list">
            {visibleStages.map((s) => (
              <PipelineStageRow
                key={s.stage}
                stage={s}
                flagged={flaggedStages.has(s.stage)}
                expanded={expandedStage === s.stage}
                onToggle={() => setExpandedStage(expandedStage === s.stage ? null : s.stage)}
              />
            ))}
            {!showAllInline && (
              <button type="button" className="pipeline-weekly-toggle" onClick={() => setWeeklyExpanded(true)}>
                ─────────── 週次工程(本日は対象外){weeklyStages.length}件 ▸ ───────────
              </button>
            )}
          </div>

          {/* D. 実行履歴 */}
          <h3>実行履歴(直近{runs.length}回)</h3>
          <div className="table-scroll">
            <table className="data-table pipeline-history-table">
              <thead>
                <tr>
                  <th>日付</th>
                  <th>状態</th>
                  <th>所要</th>
                  <th>収集</th>
                  <th>通過</th>
                  <th>スコア</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr
                    key={r.run_id}
                    className={r.run_id === run.run_id ? "pipeline-history-row-active" : "pipeline-history-row"}
                    onClick={() => selectRun(r.run_id)}
                  >
                    <td>{r.run_date}</td>
                    <td>
                      <span className={`pipeline-status-dot ${STATUS_META[r.status]?.className ?? ""}`} aria-hidden="true" />
                      {STATUS_META[r.status]?.label ?? r.status}
                    </td>
                    <td>{formatDuration(r.duration_seconds)}</td>
                    <td>{r.headline.collected?.toLocaleString() ?? "—"}</td>
                    <td>{r.headline.gated_in?.toLocaleString() ?? "—"}</td>
                    <td>{r.headline.scored?.toLocaleString() ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="pipeline-history-note">
            これより前の実行は記録がありません(記録開始:{list?.history_starts_at ?? "—"})。
          </p>
        </>
      )}
    </div>
  );
}

function PipelineStageRow({
  stage,
  flagged,
  expanded,
  onToggle,
}: {
  stage: PipelineStageView;
  flagged: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  const symbol =
    stage.status === "failed"
      ? "✕"
      : stage.status === "skipped"
        ? "−"
        : stage.status === "running"
          ? "◐"
          : flagged
            ? "!"
            : "✓";

  let summary: string;
  if (stage.status === "skipped") {
    summary = skipReasonLabel(stage.reason);
  } else if (stage.status === "failed") {
    summary = formatStageFailure(stage.reason);
  } else if (stage.status === "running") {
    summary = "実行中";
  } else {
    summary = formatStageResult(stage.stage, stage.result);
  }

  const canExpand = stage.status === "failed" && Boolean(stage.error_traceback);

  return (
    <div className={`pipeline-stage-row pipeline-stage-${stage.status}${flagged ? " pipeline-stage-flagged" : ""}`}>
      <div
        className={`pipeline-stage-row-main${canExpand ? " pipeline-stage-expandable" : ""}`}
        onClick={canExpand ? onToggle : undefined}
        role={canExpand ? "button" : undefined}
      >
        <span className="pipeline-stage-symbol" aria-hidden="true">
          {symbol}
        </span>
        <span className="pipeline-stage-sequence">{stage.sequence}</span>
        <span className="pipeline-stage-label">{stageLabel(stage.stage)}</span>
        <span className="pipeline-stage-duration">{formatDuration(stage.duration_seconds)}</span>
        <span className="pipeline-stage-summary">{summary}</span>
        {canExpand && <span className="pipeline-stage-caret">{expanded ? "▾" : "▸"}</span>}
      </div>
      {canExpand && expanded && (
        <pre className="pipeline-stage-traceback">{stage.error_traceback}</pre>
      )}
    </div>
  );
}
