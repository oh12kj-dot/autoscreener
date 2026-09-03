import { v5WarningDescription, v5WarningLabel } from "../v5Labels";

/**
 * v5専用の警告バッジ。v4の `WarningBadges.tsx` と全く同じ見た目の作法
 * (`.warning-badge-group` / `.warning-tag` / `.warning-panel` /
 * `.warning-list`)を再利用する——v5だけ浮いた見た目にならないため、かつ
 * 新規CSSを増やさないため。ラベルは `v5Labels.ts` で日本語に写像する。
 */
export function V5WarningBadges({ codes, compact = false }: { codes?: string[] | null; compact?: boolean }) {
  if (!codes || codes.length === 0) return null;

  if (compact) {
    return (
      <span className="warning-badge-group">
        {codes.map((code) => (
          <span key={code} className="warning-tag" title={v5WarningDescription(code) || code}>
            {v5WarningLabel(code)}
          </span>
        ))}
      </span>
    );
  }

  return (
    <div className="warning-panel">
      <h4>v5(shadow challenger)の warnings</h4>
      <ul className="warning-list">
        {codes.map((code) => (
          <li key={code}>
            <span className="warning-tag">{v5WarningLabel(code)}</span>
            <p>{v5WarningDescription(code) || code}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
