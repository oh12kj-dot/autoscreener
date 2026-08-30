import { useEffect, useState } from "react";
import { fetchBenchmarkReference } from "../api/client";
import type { BenchmarkReferenceResponse } from "../api/types";

export function BenchmarkReferenceSection({ horizonYears }: { horizonYears: number }) {
  const [data, setData] = useState<BenchmarkReferenceResponse | null>(null);
  useEffect(() => { fetchBenchmarkReference(horizonYears).then(setData).catch(() => setData(null)); }, [horizonYears]);
  if (!data?.quantiles) return null;
  const q = data.quantiles;
  return <p className="detail-cagr">{data.symbol} の過去{data.horizon_years}年ローリング実績: P10 {q.p10.toFixed(2)}x ・ P50 {q.p50.toFixed(2)}x ・ P90 {q.p90.toFixed(2)}x。これは過去の実績分布であり、予測ではありません。</p>;
}
