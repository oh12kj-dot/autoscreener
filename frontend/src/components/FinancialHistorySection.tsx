import { useEffect, useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchCandidateFinancials } from "../api/client";
import type { FinancialHistoryResponse, FinancialPeriodView } from "../api/types";

/** J-2(docs/investment_decision_gap_2026-08-29.md):実績の推移。
 *  `raw_snapshots.payload` に既にある財務三表を整形して見せるだけ。順位計算には一切影響しない。 */

function fmtMoney(v: number | null): string {
  if (v == null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtShares(v: number | null): string {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  return v.toLocaleString();
}

function fmtPct(v: number | null, digits = 1): string {
  return v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
}

export function FinancialHistorySection({ ticker }: { ticker: string }) {
  const [data, setData] = useState<FinancialHistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setData(null);
    fetchCandidateFinancials(ticker)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  if (error) {
    return (
      <div className="dd-section">
        <h3>実績の推移</h3>
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!data) return null;
  if (data.annual.length === 0 && data.quarterly.length === 0) {
    return (
      <div className="dd-section">
        <h3>実績の推移</h3>
        <p className="detail-cagr">この銘柄の payload に財務三表がありません。</p>
      </div>
    );
  }

  const chartData = data.annual.map((p: FinancialPeriodView) => ({
    period: p.period_end.slice(0, 7),
    revenue: p.revenue,
    gross_margin_pct: p.gross_margin != null ? p.gross_margin * 100 : null,
  }));

  const d = data.derived;
  const runwayTight =
    d.runway_months != null && d.runway_floor_months != null && d.runway_months < d.runway_floor_months;

  return (
    <div className="dd-section">
      <h3>実績の推移</h3>
      <p className="detail-cagr">
        通貨: {data.currency ?? "—"}
        {data.currency_conversion_unavailable &&
          " ・ 決算通貨と取引通貨が異なりますが換算レートが取れませんでした(下の系列は決算通貨のままです)"}
        {data.as_of && ` ・ 最新期 ${data.as_of}`}
      </p>

      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="period" />
            <YAxis yAxisId="rev" tickFormatter={(v) => fmtMoney(Number(v))} width={70} />
            <YAxis
              yAxisId="gm"
              orientation="right"
              domain={[0, "auto"]}
              tickFormatter={(v) => `${Number(v).toFixed(0)}%`}
              width={50}
            />
            <Tooltip
              formatter={(value, name) =>
                name === "売上"
                  ? [fmtMoney(Number(value)), name]
                  : [`${Number(value).toFixed(1)}%`, name]
              }
            />
            <Legend />
            <Bar yAxisId="rev" dataKey="revenue" name="売上" fill="#93c5fd" />
            <Line
              yAxisId="gm"
              type="monotone"
              dataKey="gross_margin_pct"
              name="粗利率"
              stroke="#2563eb"
              dot
              connectNulls
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}

      <p>
        売上成長(前年比) {fmtPct(d.revenue_yoy)} ・ 3年CAGR {fmtPct(d.revenue_cagr_3y)} ・ 直近粗利率{" "}
        {fmtPct(d.gross_margin_latest)}
      </p>
      <p className={runwayTight ? "warning-tag" : undefined}>
        ランウェイ:{" "}
        {d.runway_months == null
          ? d.quarterly_burn_rate == null
            ? "FCF黒字(バーンなし)"
            : "現金残高が取れず算出不能"
          : `あと約 ${d.runway_months.toFixed(0)} ヶ月`}
        {d.runway_floor_months != null && ` (増資が要る目安 ${d.runway_floor_months} ヶ月)`}
        {d.quarterly_burn_rate != null && ` ・ 四半期バーン ${fmtMoney(-d.quarterly_burn_rate)}`}
      </p>

      <table className="data-table">
        <thead>
          <tr>
            <th>期末</th>
            <th>売上</th>
            <th>粗利率</th>
            <th>営業CF</th>
            <th>FCF</th>
            <th>現金</th>
            <th>ネットデット</th>
            <th>株式数</th>
          </tr>
        </thead>
        <tbody>
          {[...data.annual].reverse().map((p) => (
            <tr key={p.period_end}>
              <td>{p.period_end}</td>
              <td>{fmtMoney(p.revenue)}</td>
              <td>{fmtPct(p.gross_margin)}</td>
              <td>{fmtMoney(p.operating_cash_flow)}</td>
              <td>{fmtMoney(p.free_cash_flow)}</td>
              <td>{fmtMoney(p.cash_and_equivalents)}</td>
              <td>{fmtMoney(p.net_debt)}</td>
              <td>{fmtShares(p.shares_outstanding)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="detail-cagr">
        株式数の年率増加率: {fmtPct(d.share_growth_rate)}(希薄化の実績。分母の推移)
      </p>

      {data.quarterly.length > 0 && (
        <>
          <h4>四半期(最大5期)</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>期末</th>
                <th>売上</th>
                <th>粗利率</th>
                <th>FCF</th>
                <th>現金</th>
              </tr>
            </thead>
            <tbody>
              {[...data.quarterly].reverse().map((p) => (
                <tr key={p.period_end}>
                  <td>{p.period_end}</td>
                  <td>{fmtMoney(p.revenue)}</td>
                  <td>{fmtPct(p.gross_margin)}</td>
                  <td>{fmtMoney(p.free_cash_flow)}</td>
                  <td>{fmtMoney(p.cash_and_equivalents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h4>予想と実績</h4>
      {!data.earnings?.covered ? <p className="detail-cagr">この銘柄をカバーしているアナリストはいません。予想対比は測れません。</p> : <>
        <p>連続 beat {data.earnings.consecutive_beats ?? "—"} 回 ・ 30日予想改訂 {fmtPct(data.earnings.estimate_revision_30d)}</p>
        <p className="detail-cagr">サプライズは予想を下げてから作れます。予想改訂の方向と一緒に読んでください。</p>
        <table className="data-table"><thead><tr><th>日付</th><th>予想</th><th>実績</th><th>差</th></tr></thead><tbody>{data.earnings.periods.map((p) => <tr key={p.date}><td>{p.date}</td><td>{p.estimate ?? "—"}</td><td>{p.reported ?? "—"}</td><td>{fmtPct(p.surprise_pct)}</td></tr>)}</tbody></table>
      </>}

      <h4>
        Piotroski F-score 内訳({d.piotroski_criteria_met}/{d.piotroski_criteria_computable} 達成
        {d.piotroski_score_ratio != null && ` ・ 充足率 ${fmtPct(d.piotroski_score_ratio, 0)}`})
      </h4>
      <p className="detail-cagr">
        成長の質の代理指標。`health_index` を通じて生存確率の推定に効いています(加点には使いません)。
      </p>
      <ul className="warning-list">
        {d.piotroski_criteria.map((c) => (
          <li key={c.key}>
            <span aria-hidden>{c.met === true ? "✅" : c.met === false ? "❌" : "—"}</span> {c.label}
            {c.met == null && <span className="detail-cagr"> (判定不能)</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
