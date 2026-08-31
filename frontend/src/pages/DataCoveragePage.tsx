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
    <div className="table-scroll"><table className="data-table"><thead><tr><th>Dataset</th><th>coverage</th><th>stale</th><th>failed</th><th>last successful</th><th>source</th></tr></thead><tbody>
      {data.datasets.map((row) => <tr key={row.dataset}><td>{row.dataset}</td><td>{pct(row.coverage)}</td><td>{pct(row.stale)}</td><td>{pct(row.failed)}</td><td>{row.last_successful ? new Date(row.last_successful).toLocaleString("ja-JP") : "—"}</td><td>{row.source ?? "—"}</td></tr>)}
    </tbody></table></div>
  </div>;
}
