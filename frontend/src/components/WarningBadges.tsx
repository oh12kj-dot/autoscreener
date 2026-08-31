import { WARNING_INFO } from "../warnings";

/**
 * 警告バッジ(C-4、docs/model_audit_v4_2026-08-26.md)。
 *
 * `compact` はランキング表のセル用(ラベルのみ、ホバーで説明)。
 * 通常表示は銘柄詳細用で、コードごとに説明文まで並べる。
 *
 * `codes` は `undefined` でも壊れないようにしてある。APIプロセスの再起動
 * 忘れで、この項目を返さない旧レスポンスと新フロントが一時的に混在する
 * ことがあり(README「トラブルシューティング」参照)、そこで `undefined.length`
 * が例外を投げて画面全体が白/黒落ちする事故が実際に起きたため。
 */
export function WarningBadges({ codes, compact = false }: { codes?: string[] | null; compact?: boolean }) {
  if (!codes || codes.length === 0) return null;

  if (compact) {
    return (
      <span className="warning-badge-group">
        {codes.map((code) => (
          <span key={code} className="warning-tag" title={WARNING_INFO[code]?.description ?? code}>
            {WARNING_INFO[code]?.label ?? code}
          </span>
        ))}
      </span>
    );
  }

  return (
    <div className="warning-panel">
      <h4>この銘柄への警告</h4>
      <ul className="warning-list">
        {codes.map((code) => (
          <li key={code}>
            <span className="warning-tag">{WARNING_INFO[code]?.label ?? code}</span>
            <p>{WARNING_INFO[code]?.description ?? ""}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
