import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { fetchWatchlist } from "../api/client";
import type { WatchlistResponse } from "../api/types";
import { Term } from "../components/Term";

const PAGE_SIZE = 50;

// 27章で `high_growth_suppressed`(高成長だが総合が伸びない)タブを削除した。
// あれは加重幾何平均が高成長銘柄を構造的に沈めるというモデル側の欠陥に対する
// 対症療法であり、実現倍率モデルではその原因自体が無くなったため。
// 高成長銘柄は今やランキング本体の上位に直接現れる。
const REASON_TABS: { key: string; label: string; help: string }[] = [
  {
    key: "single_gate_miss",
    label: "ゲート1つ未達",
    help: "除外ゲート(スコア計算の前に対象外を機械的に落とす条件)のうち1つだけを落とした銘柄。改善して候補に復帰しうるものだけを載せています(時価総額・売上高の上限超過やセクター除外は、改善を期待する類の条件ではないため対象外)。",
  },
  {
    key: "recent_listing",
    label: "新規上場",
    help: "決算データの期数が4四半期に満たない銘柄(10章)。ユニバースからは除外されますが、期数が貯まれば候補になるため追跡します。",
  },
  {
    key: "negative_outlook",
    label: "見通しがマイナス",
    help: "モデルは算出できたが、期待倍率が1.0を下回った銘柄。売上成長・利益率・マルチプル・希薄化を7年後まで外挿すると、中心的な見通しで株主価値を毀損します。順位を付けないのは、対数正規モデルではばらつきが大きいほど閾値超過確率が上がるため——見通しがマイナスの銘柄に順位を付けると「モデルが外れることに賭ける」順位づけになるからです。事業が回復すれば候補に戻ります。",
  },
  {
    key: "insufficient_data",
    label: "データ不足",
    help: "全ゲートを通過したものの、実現倍率モデルの必須入力(開示済み年次売上2期・粗利・発行済株式数・株価)が揃わずスコアを付けられなかった銘柄。欠損は悪材料ではなく「測れない」という意味なので、低いスコアを付けずにここへ回しています。",
  },
];

const GATE_LABELS: Record<string, string> = {
  dilution_ceiling: "希薄化率",
  cash_runway_floor: "キャッシュランウェイ",
  liquidity_floor: "流動性",
  price_floor: "株価下限",
  negative_equity: "自己資本",
  insufficient_listing_history: "決算実績の期数",
};

export function WatchlistPage() {
  // タブと絞り込みはURLに載せる。分類ごとのリンクを共有でき、ブラウザの
  // 戻るボタンがタブ切り替えとして機能する。
  const [searchParams, setSearchParams] = useSearchParams();
  const reason = searchParams.get("reason") ?? REASON_TABS[0].key;
  const gate = searchParams.get("gate") ?? "";
  // 29章:ランキング画面で選んだ目標(何年で何倍)を引き継ぐ。規模の上限が
  // 目標倍率の関数になったため、「あと一歩」の判定も目標によって変わる。
  const horizonYears = Number(searchParams.get("h") ?? 7);
  const targetMoic = Number(searchParams.get("m") ?? 10);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [gateCounts, setGateCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchWatchlist({ reason, gate: gate || undefined, limit: PAGE_SIZE, offset, horizonYears, targetMoic })
      .then((res) => {
        setData(res);
        setCounts(res.counts_by_reason);
        setGateCounts(res.counts_by_gate);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [reason, gate, offset, horizonYears, targetMoic]);

  useEffect(() => {
    setOffset(0);
  }, [reason, gate]);

  const activeTab = REASON_TABS.find((t) => t.key === reason);
  const showGateFilter = reason === "single_gate_miss";

  return (
    <div>
      <h2>
        監視リスト(Tier 2)
        <span className="ranking-target">
          {horizonYears}年で{targetMoic}倍
        </span>
      </h2>
      <p>
        ランキング(Tier 1)は「全ゲートを通過し、P(7年で10倍)が高い順」に並べたものです。ここにはそこに出てこないが追跡する価値がある銘柄を、理由別に分けて表示します(15.5 の二層構成)。
        {data?.snapshot_date && <span className="score-date"> 基準日: {data.snapshot_date}</span>}
      </p>

      <div className="tier2-tabs" role="tablist">
        {REASON_TABS.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={reason === tab.key}
            className={reason === tab.key ? "tier2-tab tier2-tab-active" : "tier2-tab"}
            onClick={() => {
              // 29章:目標(h/m)はタブを切り替えても保つ。ここで作り直すと
              // 「3年で3倍」で見ていたのにタブを押した瞬間に既定へ戻る。
              const next = new URLSearchParams(searchParams);
              next.set("reason", tab.key);
              next.delete("gate");
              setSearchParams(next);
              setOffset(0);
            }}
          >
            {tab.label}
            {counts[tab.key] != null && <span className="tier2-tab-count">{counts[tab.key]}</span>}
          </button>
        ))}
      </div>

      {activeTab && <p className="tier2-help">{activeTab.help}</p>}

      <p className="tier2-help">
        <Term id="tier2">監視リスト</Term>の考え方、
        <Term id="gate">除外ゲート</Term>、<Term id="expected-moic">期待倍率</Term>などの用語は
        <Link to="/glossary">用語集</Link>で説明しています。
      </p>

      {showGateFilter && (
        <div className="filters">
          <label>
            未達ゲートで絞り込み
            <select
              value={gate}
              onChange={(e) => {
                const next = new URLSearchParams(searchParams);
                next.set("reason", reason);
                if (e.target.value) {
                  next.set("gate", e.target.value);
                } else {
                  next.delete("gate");
                }
                setSearchParams(next);
                setOffset(0);
              }}
            >
              <option value="">すべて</option>
              {Object.entries(gateCounts)
                .filter(([key]) => key !== "insufficient_listing_history")
                .sort((a, b) => b[1] - a[1])
                .map(([key, count]) => (
                  <option key={key} value={key}>
                    {GATE_LABELS[key] ?? key}({count})
                  </option>
                ))}
            </select>
          </label>
        </div>
      )}

      {loading && <p>読み込み中...</p>}
      {error && <p className="error">エラー: {error}</p>}

      {data && !loading && data.items.length === 0 && <p>該当する銘柄がありません。</p>}

      {data && data.items.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>銘柄</th>
                  <th>セクター</th>
                  <th>理由</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.ticker}>
                    <td>
                      <Link
                        to={`/candidates/${item.ticker}?h=${horizonYears}&m=${targetMoic}`}
                        className="ticker-link"
                      >
                        <span className="ticker-symbol">{item.ticker}</span>
                        {item.company_name && <span className="company-name-cell">{item.company_name}</span>}
                      </Link>
                    </td>
                    <td>{item.sector ?? "—"}</td>
                    <td>
                      {item.gate && (
                        <span className="direction-badge direction-low">{GATE_LABELS[item.gate] ?? item.gate}</span>
                      )}
                      <span className="metric-formula">{item.detail}</span>
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
