import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ScoreHistoryPoint } from "../api/types";
import { fittedDomain } from "../chartScale";

/**
 * P(7年で10倍)の推移。値が0.0001〜0.05のオーダーに集中するため、
 * 固定の0〜100軸ではなく実データのレンジに合わせる(27章)。
 */
export function ScoreHistoryChart({ data }: { data: ScoreHistoryPoint[] }) {
  // score_history はAPIから新しい順で届くため、グラフ用に古い順へ並び替える
  const chartData = [...data]
    .reverse()
    .map((d) => ({ score_date: d.score_date, probability_pct: d.probability != null ? d.probability * 100 : null }));

  // 0〜100 では無く実データのレンジに合わせる(値は 0.0001〜0.05 のオーダーに集中する)。
  // 下限は 0 で止める——確率なので軸が負に食い込むと誤読を招く。
  const yDomain = fittedDomain(chartData.map((d) => d.probability_pct), { clampMin: 0 }) ?? [0, "auto"];

  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="score_date" />
        <YAxis
          domain={yDomain}
          tickFormatter={(v) => `${Number(v).toFixed(2)}%`}
          width={70}
        />
        <Tooltip formatter={(v) => [`${Number(v).toFixed(3)}%`, "P(10倍)"]} />
        <Line type="monotone" dataKey="probability_pct" stroke="#2563eb" dot={false} connectNulls />
      </LineChart>
    </ResponsiveContainer>
  );
}
