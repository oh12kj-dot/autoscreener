import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchLatestBacktest, fetchPipelineRuns, fetchUniverseStatus } from "../api/client";
import type { BacktestSummary, PipelineRunSummary, UniverseStatusResponse } from "../api/types";

/**
 * 日次ジョブの状態バナー(E-6 → §6.5、docs/daily_job_status_screen_2026-08-30.md)。
 *
 * 当初(B-6/E-6)は「収集が実行中かどうか」だけを出すバナーだった。2026-08-29 の
 * 実運用で、全銘柄隔離により収集対象が0件・スコアリング中断・提出書類収集が例外、
 * という状態でもパイプラインが終了コード0で終わり、UIが前日のランキングを平常
 * どおり出し続けた。`/pipeline` に詳細画面を作ってもそれだけでは再発を防げない
 * ——利用者はランキング画面しか開かないからである。**気づく場所は、見ている
 * 画面でなければならない。**
 *
 * - 実行中:従来どおり進捗 N/M 件(§6.5 で「残すこと」と決めた文言)
 * - 失敗・要注意:警告と `/pipeline` への導線
 * - 未実行(当日の記録が無い):スケジューラが動かなかった可能性を出す
 * - 正常:何も出さない(従来どおり邪魔をしない)
 *
 * 進捗の N/M だけは `/universe/status` から取る。収集の実行中は
 * `pipeline_stage_runs` の collection 行が `running` で `result` が未確定なので、
 * パイプラインAPIからは件数を出せない——`collection_logs` を直接数える
 * `/universe/status` にしか live の進捗は無い。役割で使い分ける
 * (状態の判定=パイプラインAPI、実行中の件数=収集API)。
 */
export function CollectionStatusBanner({ scoreDate }: { scoreDate?: string | null }) {
  // undefined = 取得前 / null = 記録なし(または取得失敗)
  const [run, setRun] = useState<PipelineRunSummary | null | undefined>(undefined);
  const [collection, setCollection] = useState<UniverseStatusResponse | null>(null);
  const [validation, setValidation] = useState<BacktestSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPipelineRuns(1)
      .then((r) => {
        if (!cancelled) setRun(r.runs[0] ?? null);
      })
      .catch(() => {
        // このバナーは補助情報。APIが落ちていても本画面の妨げにはしない
        // (API障害そのものは `/pipeline` 側が明示する)。
        if (!cancelled) setRun(null);
      });
    fetchUniverseStatus()
      .then((s) => {
        if (!cancelled) setCollection(s);
      })
      .catch(() => {
        // 進捗の件数は無くても文意が通る(下の detail が空になるだけ)。
      });
    fetchLatestBacktest().then((value) => { if (!cancelled) setValidation(value); }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  if (validation && validation.validation_status !== "PASS") {
    return <div className="collection-status-banner collection-status-banner--warn" role="alert">
      <span aria-hidden="true">⚠</span><span><strong>Research Only / validation {validation.validation_status}</strong>
      {validation.validation_reasons.length > 0 && ` — ${validation.validation_reasons.join(", ")}`}。{" "}<Link to="/validation">検証理由を見る</Link></span>
    </div>;
  }

  if (run === undefined || run === null) return null;

  if (run.status === "running") {
    // 進捗件数は `/universe/status` が取れたときだけ添える。
    const processed = collection ? Object.values(collection.collection_status_counts).reduce((a, b) => a + b, 0) : null;
    const target = collection?.collection_target_count ?? null;
    const detail =
      processed == null ? "" : target != null ? ` (${processed}/${target}件)` : ` (${processed}件処理済み)`;
    return (
      <div className="collection-status-banner" role="status">
        <span className="collection-status-dot" aria-hidden="true" />
        本日の収集を実行中です{detail}。表示中のスコアは前回実行時のものです。
      </div>
    );
  }

  // 8.4/20.3:パイプラインの `run_date` は utc_today() なので、当日判定もUTCで
  // 揃える。ブラウザのローカル日付と比べると、09:00 JST(=00:00 UTC)前後で
  // 「今日の実行が無い」と誤判定する。
  const utcToday = new Date().toISOString().slice(0, 10);
  const hasProblem = run.status === "failed" || run.status === "degraded";

  if (run.run_date !== utcToday) {
    return (
      <div className="collection-status-banner collection-status-banner--warn" role="status">
        <span aria-hidden="true">⚠</span>
        <span>
          本日の日次ジョブがまだ実行されていません(最終実行: {run.run_date}
          {hasProblem ? "、その実行にも問題がありました" : ""})。表示中のランキングは
          {scoreDate ? ` ${scoreDate} ` : "前回実行"}時点のものです。{" "}
          <Link to="/pipeline">実行状況を見る</Link>
        </span>
      </div>
    );
  }

  if (hasProblem) {
    return (
      <div className="collection-status-banner collection-status-banner--warn" role="status">
        <span aria-hidden="true">⚠</span>
        <span>
          本日の日次ジョブに問題がありました。表示中のランキングは
          {scoreDate ? ` ${scoreDate} ` : "前回実行"}時点のものです。{" "}
          <Link to="/pipeline">実行状況を見る</Link>
        </span>
      </div>
    );
  }

  return null;
}
