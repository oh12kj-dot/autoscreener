import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchCandidates, fetchScoreDates } from "../api/client";
import type { CandidateSummary } from "../api/types";

function formatProbability(p: number): string {
  const v = p * 100;
  if (v >= 1) return `${v.toFixed(1)}%`;
  if (v >= 0.01) return `${v.toFixed(2)}%`;
  return "<0.01%";
}

interface RankChange {
  ticker: string;
  companyName: string | null;
  sector: string | null;
  currentRank: number;
  previousRank: number | null;
  delta: number | null; // 正の値 = 順位上昇
  probability: number;
}

async function fetchAllCandidates(date: string): Promise<CandidateSummary[]> {
  const all: CandidateSummary[] = [];
  let offset = 0;
  const limit = 200;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const page = await fetchCandidates({ date, limit, offset });
    all.push(...page.items);
    if (all.length >= page.total || page.items.length === 0) break;
    offset += limit;
  }
  return all;
}

function computeRankChanges(current: CandidateSummary[], previous: CandidateSummary[]): RankChange[] {
  const previousRankByTicker = new Map(previous.map((p) => [p.ticker, p.rank]));
  return current.map((c) => {
    const previousRank = previousRankByTicker.get(c.ticker) ?? null;
    return {
      ticker: c.ticker,
      companyName: c.company_name,
      sector: c.sector,
      currentRank: c.rank,
      previousRank,
      delta: previousRank != null ? previousRank - c.rank : null,
      probability: c.probability,
    };
  });
}

export function RankChangesPage() {
  const [dates, setDates] = useState<string[]>([]);
  const [changes, setChanges] = useState<RankChange[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchScoreDates(2)
      .then(async ({ dates: fetchedDates }) => {
        setDates(fetchedDates);
        if (fetchedDates.length < 2) {
          setChanges(null);
          return;
        }
        const [latest, previous] = fetchedDates;
        const [currentList, previousList] = await Promise.all([fetchAllCandidates(latest), fetchAllCandidates(previous)]);
        setChanges(computeRankChanges(currentList, previousList));
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>読み込み中...</p>;
  if (error) return <p className="error">エラー: {error}</p>;

  if (dates.length < 2 || !changes) {
    return (
      <div>
        <h2>順位変動</h2>
        <p>比較可能な過去日のスコアがまだありません。日次自動実行が2日分以上蓄積されると表示されます。</p>
      </div>
    );
  }

  const newEntries = changes.filter((c) => c.previousRank == null);
  const risers = changes
    .filter((c): c is RankChange & { delta: number } => c.delta != null && c.delta > 0)
    .sort((a, b) => b.delta - a.delta)
    .slice(0, 20);

  return (
    <div>
      <h2>
        順位変動({dates[1]} → {dates[0]})
      </h2>

      <section>
        <h3>新規ランクイン({newEntries.length}件)</h3>
        {newEntries.length === 0 && <p>新規ランクインはありません。</p>}
        <ul className="change-list">
          {newEntries.slice(0, 20).map((c) => (
            <li key={c.ticker}>
              <Link to={`/candidates/${c.ticker}`} className="ticker-link">
                <span className="ticker-symbol">{c.ticker}</span>
                {c.companyName && <span className="company-name-cell">{c.companyName}</span>}
              </Link>
              <span className="change-meta">
                順位{c.currentRank}・スコア{formatProbability(c.probability)}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3>急上昇銘柄</h3>
        {risers.length === 0 && <p>順位上昇銘柄はありません。</p>}
        <ul className="change-list">
          {risers.map((c) => (
            <li key={c.ticker}>
              <Link to={`/candidates/${c.ticker}`} className="ticker-link">
                <span className="ticker-symbol">{c.ticker}</span>
                {c.companyName && <span className="company-name-cell">{c.companyName}</span>}
              </Link>
              <span className="change-meta">
                {c.previousRank}位 → {c.currentRank}位(<span className="rank-up">+{c.delta}</span>)・スコア
                {formatProbability(c.probability)}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
