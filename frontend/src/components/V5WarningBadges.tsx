import { v5WarningCategory, v5WarningDescription, v5WarningLabel } from "../v5Labels";

/**
 * v5専用の警告バッジ。v4の `WarningBadges.tsx` と全く同じ見た目の作法
 * (`.warning-badge-group` / `.warning-tag` / `.warning-panel` /
 * `.warning-list`)を再利用する——v5だけ浮いた見た目にならないため、かつ
 * 新規CSSを増やさないため。ラベルは `v5Labels.ts` で日本語に写像する。
 *
 * WP-C(docs/racr_wp_c_api_ui_2026-09-04.md):「鮮度」「モデルが構造的に
 * 未対応」「母集団のcoverage不足」「未検証」は原因が違うため、
 * `warning-tag--<category>` で色を分ける(v5Labels.tsのv5WarningCategory)。
 */
export function V5WarningBadges({ codes, compact = false }: { codes?: string[] | null; compact?: boolean }) {
  if (!codes || codes.length === 0) return null;

  if (compact) {
    return (
      <span className="warning-badge-group">
        {codes.map((code) => (
          <span
            key={code}
            className={`warning-tag warning-tag--${v5WarningCategory(code)}`}
            title={v5WarningDescription(code) || code}
          >
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
            <span className={`warning-tag warning-tag--${v5WarningCategory(code)}`}>{v5WarningLabel(code)}</span>
            <p>{v5WarningDescription(code) || code}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
