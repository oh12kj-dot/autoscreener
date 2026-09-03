import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchV5Objectives, fetchV5Scores, fetchV5ValidationStatus } from "../api/client";
import type { ModelV5ObjectivesResponse, ModelV5ScoreListResponse, ModelV5ValidationStatus } from "../api/types";
import { V5WarningBadges } from "./V5WarningBadges";
import {
  v5DecisionLabel,
  v5DistributionStatusLabel,
  v5FilterRowBoilerplateWarnings,
  v5ObjectiveLabel,
  v5SignalLabel,
} from "../v5Labels";

const PAGE_SIZE = 50;

function pct(v: number | null): string {
  if (v == null) return "—";
  const p = v * 100;
  if (p >= 1) return `${p.toFixed(1)}%`;
  if (p >= 0.01) return `${p.toFixed(2)}%`;
  return `<0.01%`;
}

/** objective の値は objective ごとに単位が違う(確率・年率・比率)。単位を
 * 一律に決め打ちしないため、選択中の objective 名から表示形式を選ぶ。 */
function formatObjectiveValue(objective: string, value: number | null): string {
  if (value == null) return "—";
  if (objective === "ten_bagger" || objective === "capital_preservation") return pct(value);
  if (objective === "expected_return" || objective === "risk_adjusted") return `${(value * 100).toFixed(1)}%`;
  return value.toFixed(3);
}

/**
 * Phase 8(Issue #3 §28・§29・§34・§36):RankingPage に切り替えて表示する
 * v5 shadow challenger の一覧。**v4 の一覧とは完全に別コンポーネント**——
 * 既存 v4 画面の表示を一切変えないため(引継ぎ書の明示要件)。
 */
export function V5RankingSection() {
  const [objectivesData, setObjectivesData] = useState<ModelV5ObjectivesResponse | null>(null);
  const [objective, setObjective] = useState<string>("");
  const [data, setData] = useState<ModelV5ScoreListResponse | null>(null);
  const [status, setStatus] = useState<ModelV5ValidationStatus | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchV5Objectives().then((res) => {
      setObjectivesData(res);
      setObjective((current) => current || res.default_objective);
    });
    fetchV5ValidationStatus().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!objective) return;
    setLoading(true);
    setError(null);
    fetchV5Scores({ objective, limit: PAGE_SIZE, offset })
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [objective, offset]);

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
          <select value={objective} onChange={(e) => { setObjective(e.target.value); setOffset(0); }}>
            {objectivesData?.objectives.map((o) => (
              <option key={o.name} value={o.name} title={o.description}>
                {v5ObjectiveLabel(o.name)}
              </option>
            ))}
          </select>
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
      {data && data.items.length === 0 && !loading && <p>該当する候補がありません。</p>}

      {data && data.items.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="data-table ranking-table">
              <thead>
                <tr>
                  <th>順位</th>
                  <th>銘柄</th>
                  <th>{v5ObjectiveLabel(objective)}</th>
                  <th>P(10x)</th>
                  <th>期待CAGR</th>
                  <th>P(loss, &lt;1.0x)</th>
                  <th>生存確率</th>
                  <th>confidence</th>
                  <th>モデル</th>
                  <th>warnings</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.ticker}>
                    <td className="rank-cell">{item.rank ?? "—"}</td>
                    <td>
                      <Link to={`/candidates/${item.ticker}`} className="ticker-link">
                        <span className="ticker-symbol">{item.ticker}</span>
                      </Link>
                    </td>
                    <td>{formatObjectiveValue(item.selected_objective, item.objective_value)}</td>
                    <td>{pct(item.distribution.p_target)}</td>
                    <td>
                      {item.distribution.expected_cagr != null
                        ? `${(item.distribution.expected_cagr * 100).toFixed(1)}%`
                        : "—"}
                    </td>
                    <td>{pct(item.distribution.p_moic_below_1_0)}</td>
                    <td>
                      {item.distribution.survival_probability != null
                        ? `${(item.distribution.survival_probability * 100).toFixed(0)}%`
                        : "—"}
                    </td>
                    <td>{(item.confidence * 100).toFixed(0)}%</td>
                    <td>v5({v5DistributionStatusLabel(item.distribution.status)})</td>
                    <td>
                      <V5WarningBadges
                        codes={v5FilterRowBoilerplateWarnings(item.warnings)}
                        compact
                      />
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
