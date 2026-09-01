import { useEffect, useState } from "react";
import { fetchDataCoverage } from "../api/client";
import type { DataCoverageResponse } from "../api/types";

const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

export function DataCoveragePage() {
  const [data, setData] = useState<DataCoverageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { fetchDataCoverage().then(setData).catch((e: Error) => setError(e.message)); }, []);
  if (error) return <p className="error">エラー: {error}</p>;
  if (!data) return <p>読み込み中...</p>;
  return <div><h2>Live Intelligence データカバレッジ</h2>
    <p className="model-notice">「未取得」と「取得済み・該当なし」をDB上で区別しています。母集団 {data.ticker_count.toLocaleString()}銘柄、基準日 {data.as_of}。</p>
    <div className="table-scroll"><table className="data-table"><thead><tr><th>Dataset</th><th>対象 / 全体</th><th>取得あり</th><th>該当なし</th><th>失敗</th><th>未試行</th><th>運用coverage</th><th>全体coverage</th><th>最終試行</th></tr></thead><tbody>
      {data.datasets.map((row) => <tr key={row.dataset}><td>{row.dataset}</td><td>{row.targeted_count.toLocaleString()} / {row.universe_count.toLocaleString()}</td><td>{row.with_data_count.toLocaleString()}</td><td>{row.no_finding_count.toLocaleString()}</td><td>{row.failed_count.toLocaleString()}</td><td>{row.not_collected_count.toLocaleString()}</td><td>{pct(row.operational_coverage)}</td><td>{pct(row.universe_coverage)}</td><td>{row.last_attempted ? new Date(row.last_attempted).toLocaleString("ja-JP") : "—"}</td></tr>)}
    </tbody></table></div>
  </div>;
}
