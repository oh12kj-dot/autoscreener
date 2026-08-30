import { useEffect, useState } from "react";
import { fetchFilings } from "../api/client";
import type { FilingListResponse } from "../api/types";
import { FORM_LABELS, ITEM_LABELS } from "../filingForms";

export function FilingsTimelineSection({ ticker }: { ticker: string }) {
  const [data, setData] = useState<FilingListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { fetchFilings(ticker, 30).then(setData).catch((e: Error) => setError(e.message)); }, [ticker]);
  return <section className="dd-section"><h3>提出書類の時系列</h3>
    {error && <p className="error">{error}</p>}
    {!data && !error && <p>読み込み中...</p>}
    {data?.total === 0 && <p className="detail-cagr">EDGAR未追跡の銘柄です。</p>}
    {data && data.total > 0 && <ul className="warning-list">{data.items.map((filing) => <li key={filing.accession_number}>
      <strong>{filing.filed_date}</strong> ・ {FORM_LABELS[filing.form] ?? filing.form}
      {filing.items.map((item) => ` ・ ${ITEM_LABELS[item] ?? item}`).join("")}
      {filing.document_url && <> ・ <a href={filing.document_url} target="_blank" rel="noreferrer">原本</a></>}
    </li>)}</ul>}
  </section>;
}
