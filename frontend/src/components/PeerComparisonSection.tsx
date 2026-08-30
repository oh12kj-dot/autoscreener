import { useEffect, useState } from "react";
import { fetchCandidatePeers } from "../api/client";
import type { PeerResponse } from "../api/types";

export function PeerComparisonSection({ ticker }: { ticker: string }) {
  const [data, setData] = useState<PeerResponse | null>(null);
  useEffect(() => { fetchCandidatePeers(ticker).then(setData).catch(() => setData(null)); }, [ticker]);
  if (!data) return null;
  if (data.peer_basis === "none") return <section className="dd-section"><h3>同業比較</h3><p className="detail-cagr">比較対象が見つかりません。</p></section>;
  return <section className="dd-section"><h3>同業比較</h3><p className="detail-cagr">比較対象: {data.peer_basis === "industry" ? "同業種" : "セクター"}の同じ日の断面 {data.peer_count}社。{data.peer_basis === "sector" && " 同業種が3社未満のためセクターまで広げています。"}</p>
    <div className="table-scroll"><table className="data-table"><thead><tr><th>銘柄</th><th>時価総額</th><th>順位</th><th>P(目標)</th><th>期待倍率</th><th>売上成長</th><th>粗利率</th></tr></thead><tbody>{data.items.map((item) => <tr key={item.ticker} style={item.ticker === ticker ? { fontWeight: 700 } : undefined}><td>{item.ticker}</td><td>{item.market_cap != null ? `$${(item.market_cap / 1e6).toFixed(0)}M` : "—"}</td><td>{item.rank ?? "—"}</td><td>{item.probability != null ? `${(item.probability * 100).toFixed(2)}%` : "—"}</td><td>{item.expected_moic?.toFixed(2) ?? "—"}x</td><td>{item.revenue_growth != null ? `${(item.revenue_growth * 100).toFixed(1)}%` : "—"}</td><td>{item.gross_margin != null ? `${(item.gross_margin * 100).toFixed(1)}%` : "—"}</td></tr>)}</tbody></table></div>
  </section>;
}
