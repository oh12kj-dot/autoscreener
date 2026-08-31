import { useEffect, useMemo, useState } from "react";
import {
  fetchInvestmentIntelligence,
  fetchJpyReturn,
  fetchLatestBacktest,
  fetchReverseValuation,
  fetchRiskSizing,
  fetchUsdJpy,
} from "../api/client";
import type { BacktestSummary, InvestmentIntelligenceResponse, ReverseValuationResponse } from "../api/types";

type Props = {
  ticker: string;
  horizonYears: number;
  expectedMoic: number | null;
  realizedVol: number | null;
  evidenceGrade: string | null;
};

const sections = [
  ["reinvestment-quality", "複利の質"],
  ["operating-kpis", "先行する事業KPI"],
  ["debt-profile", "債務満期・借換リスク"],
  ["accounting-quality", "利益の質"],
  ["capital-allocation", "資本配分"],
  ["management-incentives", "経営陣のインセンティブ"],
  ["market-opportunity", "成長余地（TAM / penetration）"],
  ["thesis-milestones", "テーゼのマイルストーン"],
  ["macro-exposure", "マクロ感応度"],
  ["mna-history", "M&A competing risk"],
] as const;

const wideSections = new Set(["operating-kpis", "thesis-milestones"]);

function pct(value: number | null | undefined) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function valueText(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return Math.abs(value) <= 2 ? value.toFixed(3) : value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return `${value.length}件`;
  return "詳細あり";
}

function DataBlock({ response }: { response: InvestmentIntelligenceResponse }) {
  if (response.coverage_status === "not_collected") return <p className="intelligence-empty">未取得</p>;
  if (response.coverage_status === "collected_no_finding") return <p>取得済み・該当なし</p>;
  const records = Array.isArray(response.data) ? response.data : [response.data];
  const visible = records.filter(Boolean).slice(0, 8) as Record<string, unknown>[];
  if (!visible.length) return <p>取得済み・該当なし</p>;
  return (
    <div className="table-scroll">
      <table className="data-table intelligence-table"><tbody>
        {visible.flatMap((record, recordIndex) =>
          Object.entries(record)
            .filter(([key, value]) => !["id", "raw_payload", "content_hash"].includes(key) && !Array.isArray(value) && (typeof value !== "object" || value == null))
            .slice(0, 10)
            .map(([key, value]) => <tr key={`${recordIndex}-${key}`}><th>{key.replaceAll("_", " ")}</th><td>{valueText(value)}</td></tr>),
        )}
      </tbody></table>
    </div>
  );
}

export function InvestmentIntelligenceSections({ ticker, horizonYears, expectedMoic, realizedVol, evidenceGrade }: Props) {
  const [reverse, setReverse] = useState<ReverseValuationResponse | null>(null);
  const [data, setData] = useState<Record<string, InvestmentIntelligenceResponse>>({});
  const [risk, setRisk] = useState<InvestmentIntelligenceResponse | null>(null);
  const [jpy, setJpy] = useState<InvestmentIntelligenceResponse[]>([]);
  const [validation, setValidation] = useState<BacktestSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    setReverse(null); setData({}); setRisk(null); setJpy([]);
    fetchReverseValuation(ticker, horizonYears).then((value) => { if (!cancelled) setReverse(value); }).catch(() => undefined);
    fetchLatestBacktest().then((value) => { if (!cancelled) setValidation(value); }).catch(() => undefined);
    Promise.allSettled(sections.map(async ([key]) => [key, await fetchInvestmentIntelligence(ticker, key)] as const)).then((results) => {
      if (cancelled) return;
      const next: Record<string, InvestmentIntelligenceResponse> = {};
      for (const result of results) if (result.status === "fulfilled") next[result.value[0]] = result.value[1];
      setData(next);
    });
    fetchRiskSizing(ticker, realizedVol, evidenceGrade ?? "C").then((value) => { if (!cancelled) setRisk(value); }).catch(() => undefined);
    if (expectedMoic != null) {
      fetchUsdJpy().then((fx) => {
        if (!fx.rate) return [];
        return Promise.all([0.9, 1.0, 1.1].map((factor) => fetchJpyReturn(ticker, expectedMoic, fx.rate!, fx.rate! * factor)));
      }).then((values) => { if (!cancelled && values) setJpy(values); }).catch(() => undefined);
    }
    return () => { cancelled = true; };
  }, [ticker, horizonYears, expectedMoic, realizedVol, evidenceGrade]);

  const reversePoints = useMemo(() => reverse?.scenarios.filter((s) => s.implied_revenue_cagr != null) ?? [], [reverse]);
  const responses = Object.values(data);
  const coverageRatio = responses.length ? responses.filter((item) => item.coverage_status === "collected_with_data" || item.coverage_status === "collected_no_finding").length / sections.length : 0;
  const coverageGrade = coverageRatio >= 0.8 ? "A" : coverageRatio >= 0.6 ? "B" : coverageRatio >= 0.3 ? "C" : "D";
  const liveAge = responses.length ? Math.max(0, ...responses.map((item) => item.data_age_days ?? 0)) : null;
  const validationStatus = validation?.validation_status ?? "STALE";

  return (
    <div className="investment-intelligence">
      <div className="dd-section decision-live-header">
        <h3>投資判断の前提</h3>
        <p className="decision-live-intro">
          以下の定性・補助データを読む前に、モデル検証・取得範囲・モデル適合性・情報鮮度を同時に確認します。
        </p>
        <div className="decision-live-grid">
          <div className="decision-live-metric">
            <span className="decision-live-label">Validation</span>
            <strong className={`decision-live-value ${validationStatus !== "PASS" ? "is-warning" : ""}`}>{validationStatus}</strong>
          </div>
          <div className="decision-live-metric">
            <span className="decision-live-label">Data coverage</span>
            <strong className={`decision-live-value ${coverageGrade === "C" || coverageGrade === "D" ? "is-warning" : ""}`}>
              {coverageGrade} / {Math.round(coverageRatio * 100)}%
            </strong>
          </div>
          <div className="decision-live-metric">
            <span className="decision-live-label">Model family</span>
            <strong className={`decision-live-value ${reverse && !reverse.model_supported ? "is-warning" : ""}`}>
              {reverse?.model_family ?? "unclassified"}
            </strong>
          </div>
          <div className="decision-live-metric">
            <span className="decision-live-label">Live intelligence</span>
            <strong className={`decision-live-value ${liveAge != null && liveAge > 30 ? "is-warning" : ""}`}>
              {liveAge == null ? "age unknown" : `${liveAge} days old`}
            </strong>
          </div>
        </div>
      </div>

      <div className="dd-section intelligence-section intelligence-section--primary">
        <h3>市場が織り込む成長</h3>
        <p className="intelligence-section-kicker"><span className="th-badge">Not used in ranking</span> Coreと同じ成長フェード・終端仮定で現在価格から逆算。時点 {reverse?.as_of ?? "—"}</p>
        {reverse && <p>MODEL FAMILY <strong>{reverse.model_family}</strong>{!reverse.model_supported && " — 専用モデル未提供・ランキング対象外候補"}</p>}
        {reversePoints.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>要求収益率</th><th>市場織込CAGR</th><th>TENXとの差</th><th>Consensusとの差</th><th>Guidanceとの差</th></tr></thead><tbody>
          {reversePoints.map((row) => <tr key={row.required_return}><td>{pct(row.required_return)}</td><td>{pct(row.implied_revenue_cagr)}</td><td>{pct(row.tenx_gap)}</td><td>{pct(row.consensus_gap)}</td><td>{pct(row.guidance_gap)}</td></tr>)}
        </tbody></table></div> : <p className="intelligence-empty">計算に必要なモデル入力が未取得です。</p>}
        {reverse?.return_distribution && <p className="detail-cagr">モデル仮定ベース: P(CAGR≥15%) {pct(reverse.return_distribution.p_cagr_15)} ・ P(2x) {pct(reverse.return_distribution.p_moic_2x)} ・ expected CAGR {pct(reverse.return_distribution.expected_cagr)}</p>}
      </div>

      {sections.map(([key, title]) => (
        <div className={`dd-section intelligence-section ${wideSections.has(key) ? "intelligence-section--wide" : ""}`} key={key}>
          <h3>{title}</h3>
          <p className="intelligence-section-kicker"><span className="th-badge">Not used in ranking</span> {data[key]?.source ?? "source未取得"} ・ as of {data[key]?.as_of ?? "—"}</p>
          {data[key] ? <DataBlock response={data[key]} /> : <p className="intelligence-empty">未取得</p>}
          {key === "macro-exposure" && <p className="detail-cagr">統計的な関連であり、因果関係を示しません。</p>}
        </div>
      ))}

      <div className="dd-section intelligence-section">
        <h3>リスク縮小後のポジション上限（preview）</h3>
        <p className="intelligence-section-kicker"><span className="th-badge">Default OFF</span> Core確率をケリー式へ入れず、既存hard capを縮小する方向だけ。</p>
        {risk ? <DataBlock response={risk} /> : <p className="intelligence-empty">未取得</p>}
      </div>

      <div className="dd-section intelligence-section">
        <h3>JPY・税引後シナリオ</h3>
        <p className="intelligence-section-kicker"><span className="th-badge">User layer</span> 企業評価と分離。為替を予測せず、現在USDJPYの±10%を表示。</p>
        {jpy.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>FX scenario</th><th>JPY pre-tax MOIC</th><th>JPY after-tax MOIC</th><th>年率IRR</th><th>損益分岐USDJPY</th></tr></thead><tbody>
          {jpy.map((row, index) => { const item = row.data as Record<string, number>; return <tr key={index}><td>{["-10%", "current", "+10%"][index]}</td><td>{item.jpy_pre_tax_moic?.toFixed(2)}x</td><td>{item.jpy_after_tax_moic?.toFixed(2)}x</td><td>{pct(item.annualized_irr)}</td><td>{item.break_even_usdjpy?.toFixed(2)}</td></tr>; })}
        </tbody></table></div> : <p className="intelligence-empty">期待倍率またはUSDJPYが未取得です。</p>}
      </div>
    </div>
  );
}
