import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fetchMacro } from "../api/client";
import type { MacroResponse } from "../api/types";

/**
 * マクロ(FRED)ページ(30.8・30.9.1)。
 *
 * **表示専用。** ここに出す値がスコアへ自動反映される経路はコード上どこにも
 * 無い(30.8.3)——マクロ値からスコアを自動調整すると、モデルの検証
 * (27.8のバックテスト)が二重に効いた交絡を含むことになる。金利上昇局面で
 * 終端マルチプル前提を保守側に置き換えるかどうかは、`config/scoring.yaml`
 * を人間が変更する形でのみ行う。
 */
export function MacroPage() {
  const [data, setData] = useState<MacroResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMacro()
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>読み込み中...</p>;
  if (error) return <p className="error">エラー: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <h2>マクロ環境</h2>
      <div className="model-notice">
        <strong>この画面の値からスコアが自動的に調整されることはありません。</strong>
        モデルの終端マルチプル前提は「今の金利環境が7年続く」ことを暗黙に置いています。
        レジームが変わったと判断したら、<code>config/scoring.yaml</code> を人間が変更してください。
      </div>

      {!data.enabled && (
        <div className="dd-section">
          <p>
            マクロ系列は未設定です。<code>.env</code> に <code>FRED_API_KEY</code> を設定すると有効になります
            (<a href="https://fred.stlouisfed.org/docs/api/api_key.html" target="_blank" rel="noreferrer">
              無料で発行できます
            </a>
            )。この機能が無くても他の画面はすべて動作します。
          </p>
        </div>
      )}

      {data.enabled &&
        data.series.map((s) => (
          <div key={s.series_id} className="dd-section">
            <h3>{s.label}</h3>
            <p>
              最新値: {s.latest_value != null ? s.latest_value.toFixed(2) : "—"}
              {s.latest_observation_date && ` (${s.latest_observation_date})`}
              {s.change_3m != null && (
                <span className="th-badge">3か月比 {s.change_3m >= 0 ? "+" : ""}{s.change_3m.toFixed(2)}pt</span>
              )}
              {s.change_1y != null && (
                <span className="th-badge">1年比 {s.change_1y >= 0 ? "+" : ""}{s.change_1y.toFixed(2)}pt</span>
              )}
            </p>
            {s.history.length > 1 && (
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={s.history}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="observation_date" tick={{ fontSize: 10 }} />
                  <YAxis width={50} tick={{ fontSize: 10 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="value" stroke="#2563eb" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        ))}
    </div>
  );
}
