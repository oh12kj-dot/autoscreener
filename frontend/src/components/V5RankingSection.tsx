import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { fetchV5Objectives, fetchV5Scores, fetchV5ValidationStatus } from "../api/client";
import type { ModelV5ObjectivesResponse, ModelV5ScoreListResponse, ModelV5ScoreSummary, ModelV5ValidationStatus } from "../api/types";
import { V5WarningBadges } from "./V5WarningBadges";
import { V5UnavailableMetric } from "./V5UnavailableMetric";
import {
  v5DecisionLabel,
  v5DistributionStatusLabel,
  v5FilterRowBoilerplateWarnings,
  v5FormatProbability,
  v5FormatRate,
  v5MetricLabel,
  v5ObjectiveLabel,
  v5SignalLabel,
  v5UnavailableReasonLabel,
} from "../v5Labels";

const PAGE_SIZE = 50;

/** objective の値は objective ごとに単位が違う(確率・年率・比率)。単位を
 * 一律に決め打ちしないため、選択中の objective 名から表示形式を選ぶ。 */
function formatObjectiveValue(objective: string, value: number | null): string {
  if (value == null) return "—";
  if (objective === "ten_bagger" || objective === "capital_preservation") return v5FormatProbability(value);
  if (objective === "expected_return" || objective === "risk_adjusted" || objective === "risk_adjusted_compounding") {
    return v5FormatRate(value);
  }
  return value.toFixed(3);
}

/** WP-C(docs/racr_wp_c_api_ui_2026-09-04.md、監査§9.1):この一覧の行1件が
 * 「まだ推定できていない」のか「実測が閾値を割った」のかを、呼び出し側が
 * 混同しないための小さなセル描画ヘルパー。値がnullなら常に
 * `V5UnavailableMetric` へ委ね、0%として出すことは絶対にしない。 */
function metricCell(value: number | null, reason: string | null | undefined) {
  if (value == null) return <V5UnavailableMetric reason={reason} />;
  return v5FormatProbability(value);
}

function freshnessLabel(warnings: string[]): string {
  if (warnings.includes("raw_snapshot_not_available_as_of")) return "スナップショットなし";
  if (warnings.includes("financial_statement_pit_is_approximate")) return "PIT近似";
  return "良好";
}

/**
 * Phase 8(Issue #3 §28・§29・§34・§36):RankingPage に切り替えて表示する
 * v5 shadow challenger の一覧。**v4 の一覧とは完全に別コンポーネント**——
 * 既存 v4 画面の表示を一切変えないため(引継ぎ書の明示要件)。
 *
 * WP-C(docs/racr_wp_c_api_ui_2026-09-04.md、監査§9.1):RACRとその周辺
 * メトリクス(CE CAGR・P(15/20/25%)・永久損失・予想MDD・信頼度/鮮度)を列に
 * 追加。P(10x)は削除せず「上方余地」列へ移した。objectiveと同じくURLの
 * queryへ状態を保存し、共有・戻るボタン・詳細ページへの引き継ぎに使う——
 * ただし**defaultはconfig(`config/objectives.yaml`)がAPI経由で返す値の
 * ままにする**。ここでハードコードしたら不変条件3(RACRを既定にしない)に
 * 反する。
 */
export function V5RankingSection() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [objectivesData, setObjectivesData] = useState<ModelV5ObjectivesResponse | null>(null);
  // objectiveのURL初期値はあくまで「前回の選択を復元する」ためのものであり、
  // URLに無ければ後段のuseEffectがAPIのdefault_objectiveへ落ち着かせる。
  const [objective, setObjectiveState] = useState<string>(() => searchParams.get("objective") ?? "");
  const [data, setData] = useState<ModelV5ScoreListResponse | null>(null);
  const [status, setStatus] = useState<ModelV5ValidationStatus | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // フィルタ:distribution契約が実際に埋めているフィールドだけを対象にする
  // (計画の不変条件2:未実装メトリクス=永久損失・MDDへのフィルタは作らない)。
  const [minConfidencePct, setMinConfidencePct] = useState("");
  const [sector, setSector] = useState("");
  const [minPCagr20Pct, setMinPCagr20Pct] = useState("");

  const setObjective = (next: string) => {
    setObjectiveState(next);
    const params = new URLSearchParams(searchParams);
    params.set("objective", next);
    setSearchParams(params, { replace: true });
    setOffset(0);
  };

  useEffect(() => {
    fetchV5Objectives().then((res) => {
      setObjectivesData(res);
      // 不変条件3:defaultはAPI(=config/objectives.yaml)が言う値のみを使う。
      // ここに "risk_adjusted_compounding" 等を直書きしない。
      setObjectiveState((current) => current || res.default_objective);
    });
    fetchV5ValidationStatus().then(setStatus).catch(() => undefined);
  }, []);

  // WP-C:URLに `as_of` が乗っていれば(将来のas-ofセレクタ、または手動で
  // 付けて共有されたリンク)それを尊重する。既定(未指定)は常に最新runの
  // ままで、ここで勝手に特定の日付をURLへ書き込むことはしない——「常に
  // 最新を見る」という現状のUXを変えないため。
  const asOf = searchParams.get("as_of") || undefined;

  useEffect(() => {
    if (!objective) return;
    setLoading(true);
    setError(null);
    fetchV5Scores({
      objective,
      asOf,
      limit: PAGE_SIZE,
      offset,
      minConfidence: minConfidencePct ? Number(minConfidencePct) / 100 : undefined,
      sector: sector || undefined,
      minPCagrAbove20: minPCagr20Pct ? Number(minPCagr20Pct) / 100 : undefined,
    })
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [objective, asOf, offset, minConfidencePct, sector, minPCagr20Pct]);

  const filtersActive = Boolean(minConfidencePct || sector || minPCagr20Pct);

  return (
    <div className="v5-ranking-section">
      <div className="model-notice v5-not-for-production-notice">
        <strong>v5 は独立した shadow challenger であり、投資判断に使える品質ではありません。</strong>{" "}
        v4(Champion)は現在も本番のランキングであり、この画面は切り替え表示のみです。
        v5 には 7 年ホライズンでの実現アウトカムに基づく backtest がまだ存在しません
        （<Link to="/validation">検証状況</Link>を参照）。
      </div>

      {status && (
        <div className="v5-status-strip">
          <span>
            判定: <strong>{v5DecisionLabel(status.decision)}</strong>（{status.decision_entry_date}）
          </span>
          <span>
            評価日数: <strong>{status.evaluation_dates_count}</strong>
            {status.evaluation_date_range && (
              <> （{status.evaluation_date_range[0]} 〜 {status.evaluation_date_range[1]}）</>
            )}
          </span>
          <span>
            実現アウトカム件数: <strong>{status.realized_forward_validation_count}</strong>
          </span>
          {status.unsupported_historical_features.length > 0 && (
            <span>
              過去再現(historical backtest)非対応の特徴量:{" "}
              <strong>
                {status.unsupported_historical_features.map((k) => v5SignalLabel(k)).join("、")}
              </strong>
            </span>
          )}
        </div>
      )}

      <div className="filters">
        <label>
          Objective
          <select value={objective} onChange={(e) => setObjective(e.target.value)}>
            {objectivesData?.objectives.map((o) => (
              <option key={o.name} value={o.name} title={o.description}>
                {v5ObjectiveLabel(o.name)}
              </option>
            ))}
          </select>
        </label>
        <label>
          信頼度下限
          <input
            type="number" min={0} max={100} step={5} value={minConfidencePct}
            onChange={(e) => { setMinConfidencePct(e.target.value); setOffset(0); }}
            placeholder="例: 50"
          />
        </label>
        <label>
          セクター
          <input
            value={sector}
            onChange={(e) => { setSector(e.target.value); setOffset(0); }}
            placeholder="例: Technology"
          />
        </label>
        <label>
          {v5MetricLabel("p_cagr_above_20")}下限
          <input
            type="number" min={0} max={100} step={5} value={minPCagr20Pct}
            onChange={(e) => { setMinPCagr20Pct(e.target.value); setOffset(0); }}
            placeholder="例: 20"
          />
        </label>
        {/* WP-C:計画の不変条件2「未実装メトリクスへのフィルタを作らない」。
            永久損失・MDDのフィルタはAPIに存在しないため、無効化した状態で
            理由だけ示す——「そもそも列が無い」より「なぜ選べないか」が
            分かるほうが利用者の誤解が少ない。 */}
        <label className="v5-disabled-filter" title={v5UnavailableReasonLabel("competing_risk_model_not_implemented")}>
          {v5MetricLabel("p_permanent_loss")}上限
          <input type="number" disabled placeholder="未実装" />
        </label>
        <label className="v5-disabled-filter" title={v5UnavailableReasonLabel("path_simulation_not_implemented")}>
          {v5MetricLabel("p_mdd_above_50")}上限
          <input type="number" disabled placeholder="未実装" />
        </label>
      </div>

      {data?.run && (
        <p className="score-date">
          v5 run: {data.run.run_id.slice(0, 8)}… ・ as_of {data.run.as_of} ・
          母集団 {data.run.population_count}銘柄 ・ config_hash {data.run.config_hash.slice(0, 12)}
          {data.run.warnings.length > 0 && (
            <span className="v5-run-warnings">
              {" "}
              ・ <V5WarningBadges codes={data.run.warnings} compact />
            </span>
          )}
        </p>
      )}

      {loading && <p>読み込み中...</p>}
      {error && <p className="error">エラー: {error}</p>}

      {/* WP-Cの3つの空状態(1) この run はこの objective をまだ計算していない
          ——空のランキング表(「該当なし」)と混同させない、はっきりした
          専用メッセージにする。 */}
      {data && !data.objective_computed_for_run && (
        <div className="v5-objective-not-computed">
          <strong>この run(as_of {data.run.as_of})は「{v5ObjectiveLabel(objective)}」をまだ計算していません。</strong>
          <p>
            このrunはこのobjectiveのcontractが追加される前に実行されたものです。「候補が0件」という意味ではありません。
            別のobjectiveを選ぶか、より新しいrunを待ってください。
          </p>
        </div>
      )}

      {/* (2) objectiveは計算済みだが、フィルタ/母集団の結果として0件
          (3) 正真正銘0件、の2つはどちらも同じ文面で構わない——
          objective_computed_for_run===false のケースとだけ区別できていれば
          「候補が無い」という意味は正しく伝わる。 */}
      {data && data.objective_computed_for_run && data.items.length === 0 && !loading && (
        <p>
          該当する候補がありません。
          {filtersActive && "(フィルタ条件を満たす銘柄がありませんでした)"}
        </p>
      )}

      {data && data.objective_computed_for_run && data.items.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="data-table ranking-table v5-ranking-table">
              <thead>
                <tr>
                  <th>順位</th>
                  <th>銘柄</th>
                  <th title="explanation.omitted_terms: ドローダウン・永久損失は未実装のためこのスコアに反映されていません(詳細ページ参照)">
                    {v5ObjectiveLabel(objective)}
                  </th>
                  <th title="確実性等価CAGR。分散・破綻確率を織り込んだ複利年率(RACRの起点になる値)">
                    {v5MetricLabel("ce_cagr")}
                  </th>
                  <th>期待CAGR</th>
                  <th>{v5MetricLabel("median_cagr")}</th>
                  <th>P(15/20/25%)</th>
                  <th title="旧P(10x)。目標倍率到達確率(上方の余地の目安、objectiveには使われない)">
                    上方余地 P(10x)
                  </th>
                  <th title={v5UnavailableReasonLabel("competing_risk_model_not_implemented")}>
                    {v5MetricLabel("p_permanent_loss")}
                  </th>
                  <th title={v5UnavailableReasonLabel("path_simulation_not_implemented")}>
                    {v5MetricLabel("expected_max_drawdown")}
                  </th>
                  <th>信頼度・鮮度</th>
                  <th>warnings</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item: ModelV5ScoreSummary) => (
                  <tr key={item.ticker}>
                    <td className="rank-cell">{item.rank ?? "—"}</td>
                    <td>
                      <Link
                        to={`/candidates/${item.ticker}?model=v5&objective=${encodeURIComponent(objective)}${asOf ? `&as_of=${encodeURIComponent(asOf)}` : ""}`}
                        className="ticker-link"
                      >
                        <span className="ticker-symbol">{item.ticker}</span>
                      </Link>
                    </td>
                    <td>{formatObjectiveValue(item.selected_objective, item.objective_value)}</td>
                    <td>{v5FormatRate(item.distribution.ce_cagr)}</td>
                    <td>{v5FormatRate(item.distribution.expected_cagr)}</td>
                    <td>{v5FormatRate(item.distribution.median_cagr)}</td>
                    <td className="v5-triplet-cell">
                      <span>15%: {v5FormatProbability(item.distribution.p_cagr_above_15)}</span>
                      <span>20%: {v5FormatProbability(item.distribution.p_cagr_above_20)}</span>
                      <span>25%: {v5FormatProbability(item.distribution.p_cagr_above_25)}</span>
                    </td>
                    <td>{v5FormatProbability(item.distribution.p_target)}</td>
                    <td>{metricCell(item.distribution.p_permanent_loss, item.distribution.p_permanent_loss_unavailable_reason)}</td>
                    <td>
                      {item.distribution.expected_max_drawdown == null
                        ? <V5UnavailableMetric reason={item.distribution.expected_max_drawdown_unavailable_reason} />
                        : v5FormatProbability(item.distribution.expected_max_drawdown)}
                    </td>
                    <td>
                      {(item.confidence * 100).toFixed(0)}% ・ {freshnessLabel(item.warnings)}
                    </td>
                    <td>
                      <V5WarningBadges
                        codes={v5FilterRowBoilerplateWarnings(item.warnings)}
                        compact
                      />
                      <span className="th-badge" title={`distribution.status = ${item.distribution.status}`}>
                        v5({v5DistributionStatusLabel(item.distribution.status)})
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              前へ
            </button>
            <span>
              {offset + 1}〜{Math.min(offset + PAGE_SIZE, data.total)} / {data.total}件
            </span>
            <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              次へ
            </button>
          </div>
        </>
      )}
    </div>
  );
}
