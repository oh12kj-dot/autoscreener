import { useEffect, useState } from "react";
import { fetchLlmAnalysis } from "../api/client";
import type { LlmSourceRef, LlmTickerAnalysisResponse } from "../api/types";
import { LlmMarkdown } from "./LlmMarkdown";

/** K-9:LLM(Claude)による定性分析。銘柄詳細の最下部に置く。
 *
 * **置き場所を最下部にしているのは意図的である。** 上に置くと、読み手は
 * 定量モデルの出力より先に生成文を読むことになり、順位の根拠だと受け取る。
 * ここに出るものは順位にも除外にも一切入っていない参考情報なので、
 * 一次情報(財務推移・提出書類)を見たあとに読む位置に置く。
 *
 * 未生成でもエラーにしない——生成には課金が伴うので、**作っていないのが
 * 既定の状態**である。何も無いときは、作り方(CLIコマンド)を案内する。
 */

const CONVICTION_LABELS: Record<string, string> = {
  low: "低(定型文が多く実体が読み取りにくい)",
  medium: "中",
  high: "高(開示が具体的で事業構造を追える)",
};

const SECTION_LABELS: Record<string, string> = {
  item1: "Item 1 事業",
  item1a: "Item 1A リスク要因",
  item3: "Item 3 係争",
  item7: "Item 7 MD&A",
  ex99: "EX-99 プレスリリース",
};

function sourceLabel(ref: LlmSourceRef): string {
  const parts = [ref.form, ref.section ? (SECTION_LABELS[ref.section] ?? ref.section) : null, ref.filed_date];
  return parts.filter(Boolean).join(" ・ ");
}

function SourceLinks({ refs }: { refs: LlmSourceRef[] }) {
  if (refs.length === 0) return null;
  return (
    <p className="llm-sources">
      根拠:
      {refs.map((ref, i) => (
        <span key={i}>
          {i > 0 && " / "}
          {ref.source_url ? (
            <a href={ref.source_url} target="_blank" rel="noreferrer">
              {sourceLabel(ref)}
            </a>
          ) : (
            sourceLabel(ref)
          )}
        </span>
      ))}
    </p>
  );
}

export function LlmAnalysisSection({ ticker }: { ticker: string }) {
  const [data, setData] = useState<LlmTickerAnalysisResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchLlmAnalysis(ticker)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [ticker]);

  if (loading) return null;
  if (error) {
    // 生成AIの参考情報が取れないことで画面全体を壊さない(他は全部一次情報)。
    return (
      <div className="dd-section">
        <h3>定性分析(生成AI・参考)</h3>
        <p className="error">読み込めませんでした: {error}</p>
      </div>
    );
  }
  if (!data) return null;

  const empty = data.summaries.length === 0 && data.qualitative == null;

  return (
    <div className="dd-section">
      <h3>定性分析(生成AI・参考)</h3>
      <p className="llm-disclaimer">{data.disclaimer}</p>

      {empty && (
        <p className="ticker-meta">
          この銘柄の定性分析はまだ作られていません(生成すると課金が発生するため、既定では作りません)。
          作るには次を実行します:
          <br />
          <code>uv run python -m autoscreener.cli summarize-filings --symbols {data.ticker}</code>
          <br />
          <code>uv run python -m autoscreener.cli score-qualitative --symbols {data.ticker}</code>
        </p>
      )}

      {data.qualitative && (
        <div className="llm-qualitative">
          <h4>
            開示の具体性:{" "}
            <span className="warning-tag">
              {CONVICTION_LABELS[data.qualitative.conviction ?? ""] ?? data.qualitative.conviction ?? "—"}
            </span>
          </h4>
          <p className="ticker-meta">
            <strong>これは投資妙味の評価ではありません。</strong>
            「開示が具体的で事業構造を追えるか」だけを見た順序尺度です(点数ではないので合成できません)。
            {data.qualitative.conviction_rationale && ` — ${data.qualitative.conviction_rationale}`}
          </p>
          {data.qualitative.business_summary && <p>{data.qualitative.business_summary}</p>}

          {data.qualitative.moat_evidence.length > 0 && (
            <>
              <h4>参入障壁について原文が述べていること</h4>
              <ul>
                {data.qualitative.moat_evidence.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {data.qualitative.key_risks.length > 0 && (
            <>
              <h4>原文が挙げているリスク</h4>
              <ul>
                {data.qualitative.key_risks.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {data.qualitative.evidence_gaps.length > 0 && (
            <>
              <h4>この抜粋では判断できない点(人間が調べる)</h4>
              <ul>
                {data.qualitative.evidence_gaps.map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            </>
          )}
          <SourceLinks refs={data.qualitative.source_refs} />
        </div>
      )}

      {data.summaries.map((summary) => (
        <details key={summary.source_key} className="llm-summary">
          <summary>
            提出書類の要約:{" "}
            {summary.source_refs.length > 0 ? sourceLabel(summary.source_refs[0]) : summary.source_key}
            <span className="th-badge">
              {summary.model} / effort {summary.effort}
            </span>
          </summary>
          <LlmMarkdown content={summary.content} />
          <SourceLinks refs={summary.source_refs} />
        </details>
      ))}
    </div>
  );
}
