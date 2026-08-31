import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchInvestmentIntelligence, fetchPositions } from "../api/client";
import type { PositionsResponse } from "../api/types";
import { useCurrency } from "../currency";
import { Term } from "../components/Term";

/**
 * 保有一覧(30.7.5・30.9.1)。
 *
 * `config/positions.yaml` が存在しない状態でも空リストと200が返る(30.7.6)。
 * その場合はエラーではなく「保有銘柄がまだ登録されていません」と案内する
 * ——ファイルが無いことと障害を混同しない。
 */
export function PositionsPage() {
  const [data, setData] = useState<PositionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [milestones, setMilestones] = useState<Record<string, Record<string, unknown> | null>>({});
  const { formatMoney } = useCurrency(); // J-10:円換算表示

  useEffect(() => {
    setLoading(true);
    fetchPositions()
      .then((value) => {
        setData(value);
        return Promise.allSettled(value.items.filter((item) => item.closed_on == null).map(async (item) => {
          const response = await fetchInvestmentIntelligence(item.ticker, "thesis-milestones");
          const rows = Array.isArray(response.data) ? response.data as Record<string, unknown>[] : [];
          return [item.ticker, rows.find((row) => row.status === "pending") ?? rows[0] ?? null] as const;
        }));
      })
      .then((results) => {
        if (!results) return;
        const next: Record<string, Record<string, unknown> | null> = {};
        for (const result of results) if (result.status === "fulfilled") next[result.value[0]] = result.value[1];
        setMilestones(next);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>読み込み中...</p>;
  if (error) return <p className="error">エラー: {error}</p>;
  if (!data) return null;

  const openItems = data.items.filter((p) => p.closed_on == null);
  const closedItems = data.items.filter((p) => p.closed_on != null);

  return (
    <div>
      <h2>保有銘柄</h2>
      <p className="ticker-meta">
        <code>config/positions.yaml</code> を読み取って表示しています。
        アプリはこのファイルを読むだけで、書き込みません(追加・売却は手でファイルを編集し、gitにコミットしてください)。
      </p>
      {/* J-8:必ず画面に書く一文(config/monitoring.yaml 冒頭と同じ立場) */}
      <p className="detail-cagr">
        「次の計画」「テーゼ点灯」の閾値は<strong>売却条件ではありません</strong>。点灯は
        「価格に関係なく判断をやり直す」合図であり、機械的な売りシグナルとして使ってはなりません。
      </p>

      {data.items.length === 0 && (
        <div className="dd-section">
          <p>保有銘柄がまだ登録されていません。</p>
          <p className="detail-cagr">
            <code>config/positions.yaml</code> に銘柄を追加すると、ここに表示されます。
          </p>
        </div>
      )}

      {data.items.length > 0 && (
        <div className="dd-section">
          <h3>ポートフォリオ集計</h3>
          <p>
            保有件数: {data.summary.position_count} ・ 総取得コスト: {formatMoney(data.summary.total_cost_usd)}
          </p>
          {data.summary.unprofitable_share != null && (
            <p>赤字銘柄の比率: {(data.summary.unprofitable_share * 100).toFixed(0)}%</p>
          )}
          {Object.keys(data.summary.sector_weights).length > 0 && (
            <ul className="warning-list">
              {Object.entries(data.summary.sector_weights).map(([sector, weight]) => (
                <li key={sector}>
                  {sector}: {(weight * 100).toFixed(1)}%
                  {data.summary.sector_cap_breaches.includes(sector) && (
                    <span className="warning-tag red-flag-blocking">セクター上限超過</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {data.summary.position_cap_breaches.length > 0 && (
            <p className="warning-tag red-flag-blocking">
              銘柄あたり上限を超過: {data.summary.position_cap_breaches.join(", ")}
            </p>
          )}
          {/* J-9:保有群のポートフォリオ見通し(相関込み) */}
          {data.cash_ratio != null && (
            <p>現金比率(取得原価ベース): {(data.cash_ratio * 100).toFixed(0)}%</p>
          )}
          {data.portfolio && (
            <p>
              保有群の見通し:{" "}
              期待的中数 {data.portfolio.expected_hits.toFixed(2)} ・ 少なくとも1つ当たる確率{" "}
              {(data.portfolio.probability_at_least_one * 100).toFixed(1)}%
              <span className="detail-cagr">
                {" "}
                (独立と仮定すると {(data.portfolio.probability_at_least_one_if_independent * 100).toFixed(1)}%。
                相関 {data.portfolio.asset_correlation.toFixed(2)} を織り込むと下がります)
              </span>
            </p>
          )}
          {data.ranking_overlap.length > 0 && (
            <p className="detail-cagr">
              現在のランキング上位と重複: {data.ranking_overlap.join("、")}
              (同じテーゼに二重に賭けていないか確認してください)
            </p>
          )}
          {data.correlations.filter((x) => x.correlation >= 0.6).length > 0 && (
            <p className="detail-cagr">実質的に同じ賭けになっている可能性: {data.correlations.filter((x) => x.correlation >= 0.6).map((x) => `${x.a}—${x.b} ${x.correlation.toFixed(2)} (${x.overlap_days}日)`).join(" / ")}</p>
          )}
        </div>
      )}

      {openItems.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>銘柄</th>
                <th>取得日</th>
                <th>株数</th>
                <th>取得単価</th>
                <th>現在値</th>
                <th>含み損益</th>
                <th>P(目標達成)</th>
                <th>達成倍率</th>
                <th>残り</th>
                <th>次の計画</th>
                <th>次のマイルストーン</th>
                <th>テーゼ点灯</th>
                <th>
                  <Term id="red-flags">未解消アラート</Term>
                </th>
                <th>ノート</th>
              </tr>
            </thead>
            <tbody>
              {openItems.map((p) => (
                <tr key={p.ticker}>
                  <td>
                    <Link to={`/candidates/${p.ticker}`} className="ticker-link">
                      <span className="ticker-symbol">{p.ticker}</span>
                    </Link>
                    {p.binary_event && <span className="th-badge">二値イベント</span>}
                  </td>
                  <td>
                    {milestones[p.ticker] ? <span>
                      {String(milestones[p.ticker]?.metric_code ?? "—")} ・ 残り {String(milestones[p.ticker]?.days_until ?? "—")}日
                      <span className="detail-cagr"> base {String(milestones[p.ticker]?.base_threshold ?? "—")} / 実績 {String(milestones[p.ticker]?.actual_value ?? "未確定")}</span>
                    </span> : "未設定"}
                  </td>
                  <td>{p.opened_on}</td>
                  <td>{p.shares.toLocaleString()}</td>
                  <td>${p.cost_basis_usd.toFixed(2)}</td>
                  <td>{p.current_price != null ? `$${p.current_price.toFixed(2)}` : "—"}</td>
                  <td>
                    {p.unrealized_return != null ? (
                      <span className={p.unrealized_return >= 0 ? "factor-contribution positive" : "factor-contribution negative"}>
                        {(p.unrealized_return * 100).toFixed(1)}%
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{p.probability != null ? `${(p.probability * 100).toFixed(2)}%` : "—"}</td>
                  <td>{p.achieved_moic != null ? `${p.achieved_moic.toFixed(2)}x` : "—"}</td>
                  <td>{p.remaining_moic_to_target != null ? `${p.remaining_moic_to_target.toFixed(1)}x / あと ${p.remaining_years?.toFixed(1) ?? "—"}年 / 年率 ${p.required_cagr_from_here != null ? `${(p.required_cagr_from_here * 100).toFixed(0)}%` : "—"}` : "—"}</td>
                  <td>
                    {p.next_trim != null ? (
                      <span title={p.next_trim.action ?? undefined}>
                        {p.next_trim.at_moic.toFixed(1)}x で{" "}
                        {p.next_trim.remaining_multiple != null &&
                          `(あと ${p.next_trim.remaining_multiple.toFixed(2)}x)`}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {p.thesis_break_hits.length > 0 ? (
                      <span className="warning-tag">{p.thesis_break_hits.join("、")}</span>
                    ) : (
                      p.thesis_evaluation_state === "unassessed" ? "未評価" : "なし"
                    )}
                  </td>
                  <td>
                    {p.open_alert_count > 0 ? (
                      <span className="warning-tag red-flag-blocking">{p.open_alert_count}件</span>
                    ) : (
                      "なし"
                    )}
                  </td>
                  <td>
                    {p.note_exists
                      ? p.note_is_complete
                        ? "記入済み"
                        : `未完成(不足: ${p.note_missing_fields.join(", ")})`
                      : "未作成"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {closedItems.length > 0 && (
        <div className="dd-section">
          <h3>売却済み(記録用)</h3>
          <ul className="warning-list">
            {closedItems.map((p) => (
              <li key={p.ticker}>
                {p.ticker}: {p.opened_on} 〜 {p.closed_on}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
