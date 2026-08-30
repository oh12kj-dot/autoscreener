import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchAlerts } from "../api/client";
import type { AlertsResponse } from "../api/types";

const SEVERITIES = ["", "blocking", "warning", "info"] as const;

/** 直近アラート一覧(30.7.5・30.9.1)。重大度で絞り込み、各行から銘柄詳細へ。 */
export function AlertsPage() {
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("");
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AlertsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchAlerts({ severity: severity || undefined, days })
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [severity, days]);

  return (
    <div>
      <h2>アラート</h2>
      <p className="ticker-meta">
        保有・追跡銘柄で新たに点灯した監視項目です。既定では未解消のもの(確認していないもの)だけを表示します。
        確認したら <code>tenx ack &lt;id&gt;</code> で確認済みにできます(APIは読み取り専用のため画面からは操作できません)。
      </p>

      <div className="filters">
        <label>
          重大度
          <select value={severity} onChange={(e) => setSeverity(e.target.value as typeof severity)}>
            <option value="">すべて</option>
            <option value="blocking">blocking</option>
            <option value="warning">warning</option>
            <option value="info">info</option>
          </select>
        </label>
        <label>
          期間(日)
          <input type="number" value={days} onChange={(e) => setDays(Number(e.target.value) || 30)} />
        </label>
      </div>

      {loading && <p>読み込み中...</p>}
      {error && <p className="error">エラー: {error}</p>}
      {data && data.items.length === 0 && !loading && <p>該当するアラートはありません。</p>}

      {data && data.items.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>銘柄</th>
                <th>コード</th>
                <th>重大度</th>
                <th>種別</th>
                <th>点灯日</th>
                <th>詳細</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td>
                    <Link to={`/candidates/${a.ticker}`} className="ticker-link">
                      <span className="ticker-symbol">{a.ticker}</span>
                    </Link>
                  </td>
                  <td>{a.code}</td>
                  <td>
                    <span className={`warning-tag ${a.severity === "blocking" ? "red-flag-blocking" : ""}`}>
                      {a.severity}
                    </span>
                  </td>
                  <td>{a.source === "premortem" ? "プレモーテム反証" : a.source === "red_flag" ? "レッドフラグ" : "監視指標"}</td>
                  <td>{a.triggered_on}</td>
                  <td>{typeof a.detail?.detail === "string" ? a.detail.detail : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
