import { useEffect, useState } from "react";
import { fetchResearchNote } from "../api/client";
import type { CandidateDetail, ResearchNoteResponse } from "../api/types";
import { deriveChecklist, externalLinks, type ChecklistState } from "../dueDiligence";
import { Term } from "./Term";

/** J-5(docs/investment_decision_gap_2026-08-29.md):デューデリ・チェックリスト(11工程)。
 *  バックエンド変更ゼロ。既存の `/candidates/{ticker}` と `/research/{ticker}` だけで状態が決まる。 */

const STATE_LABEL: Record<ChecklistState, string> = {
  auto: "自動判定済み",
  recorded: "ノート記録済み",
  todo: "未着手",
};

const STATE_MARK: Record<ChecklistState, string> = {
  auto: "✅",
  recorded: "📝",
  todo: "⬜",
};

export function DueDiligenceChecklist({ detail }: { detail: CandidateDetail }) {
  const [note, setNote] = useState<ResearchNoteResponse | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    fetchResearchNote(detail.ticker)
      .then((n) => {
        if (!cancelled) setNote(n);
      })
      .catch(() => {
        if (!cancelled) setNote(null);
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [detail.ticker]);

  if (!loaded) return null;

  const items = deriveChecklist(detail, note);
  const { links, cikUnresolved } = externalLinks(detail);
  const todoCount = items.filter((i) => i.state === "todo").length;

  return (
    <div className="dd-section">
      <h3>
        <Term id="due-diligence">デューデリ・チェックリスト</Term>
      </h3>
      <p className="detail-cagr">
        機械が測れるところ(1〜4・10)は自動判定、人間が書くところ(5〜9・11)は
        投資ノート(research/{detail.ticker}.md)の記入状況です。
        {todoCount > 0 ? (
          <>
            {" "}
            <strong>未着手が {todoCount} 件あります。</strong>元の運用ルールでは、埋められない工程が
            残っているうちは建てません。
          </>
        ) : (
          " 未着手はありません。"
        )}
      </p>

      <ol className="dd-checklist">
        {items.map((item) => (
          <li key={item.step} className={`dd-checklist-item dd-state-${item.state}${item.warn ? " dd-warn" : ""}`}>
            <span className="dd-check-mark" aria-hidden>
              {STATE_MARK[item.state]}
            </span>
            <span className="dd-check-body">
              <strong>
                {String(item.step).padStart(2, "0")}. {item.title}
              </strong>
              <span className="dd-check-state"> — {STATE_LABEL[item.state]}</span>
              <br />
              <span className="detail-cagr">{item.detail}</span>
            </span>
          </li>
        ))}
      </ol>

      <h4>一次情報への導線</h4>
      {cikUnresolved && (
        <p className="detail-cagr">CIK 未解決(refresh-cik-map を実行)。EDGAR へのリンクは出していません。</p>
      )}
      <ul className="warning-list">
        {links.map((link) => (
          <li key={link.href}>
            <a href={link.href} target="_blank" rel="noreferrer">
              {link.label}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
