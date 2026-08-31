import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchCalendar } from "../api/client";
import type { CalendarResponse } from "../api/types";

/** J-6(docs/investment_decision_gap_2026-08-29.md):カタリスト・カレンダー。
 *  次回決算日(yfinance)とノートの検証日を近い順に並べる。
 *  **「決算前に建てるな」とは書かない**——それは判断であり、アプリは日数だけを出す。 */

const EVENT_LABEL: Record<string, string> = {
  earnings: "次回決算",
  verification: "検証日(ノート)",
  manual: "手動",
};

export function CalendarPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<CalendarResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    setData(null);
    fetchCalendar(days)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [days]);

  return (
    <div>
      <h2>カタリスト・カレンダー</h2>
      <p className="ticker-meta">
        次回決算日(yfinance の推定)と、投資ノートの <code>verification_date</code> を近い順に表示します。
        日数だけを出します——決算前後の建玉タイミングは利用者の判断です。
      </p>
      <p>
        表示期間:{" "}
        {[14, 30, 60, 90].map((d) => (
          <button
            key={d}
            type="button"
            className={`link-button${d === days ? " active" : ""}`}
            style={{ marginRight: "0.6rem" }}
            onClick={() => setDays(d)}
          >
            {d}日
          </button>
        ))}
      </p>

      {error && <p className="error">{error}</p>}
      {data && data.items.length === 0 && (
        <div className="dd-section">
          <p>この期間に登録されたイベントはありません。</p>
          <p className="detail-cagr">
            <code>uv run python -m autoscreener.cli collect-events</code> で次回決算日を収集できます。
          </p>
        </div>
      )}
      {data && data.items.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>銘柄</th>
                <th>種類</th>
                <th>日付</th>
                <th>あと</th>
                <th>推定</th>
                <th>取得元</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={`${e.ticker}-${e.event_type}-${e.event_date}`}>
                  <td>
                    <Link to={`/candidates/${e.ticker}`} className="ticker-link">
                      <span className="ticker-symbol">{e.ticker}</span>
                    </Link>
                  </td>
                  <td>{EVENT_LABEL[e.event_type] ?? e.event_type}</td>
                  <td>{e.event_date}</td>
                  <td>{e.days_until}日</td>
                  <td>{e.is_estimated ? "推定" : "確定"}</td>
                  <td>{e.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
