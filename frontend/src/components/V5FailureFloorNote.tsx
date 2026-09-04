/**
 * WP-B2(docs/racr_wp_b2_risk_terms_2026-09-04.md B2-3;診断
 * docs/racr_shadow_run_diagnostic_2026-09-04.md §4)専用コンポーネント:
 * `ce_cagr_failure_floor`(既定 0.01)は「破綻・非回収的上場廃止時に元本の
 * 1%が回収される」という**実測ではない保守的な仮置きの定数**であり、
 * `ce_cagr`・`RACR` の絶対水準を丸ごと支配する——診断の実測では、この
 * floorだけを 0.01→0.50 に動かすと母集団の中央値CE CAGRが -16.6%→-6.2%
 * (10.5pt)動いた。
 *
 * `V5UnavailableMetric` と同じ「隠さない」方針・同じトーン(点線下線+
 * title属性)を踏襲するが、別コンポーネントにしている——これは「未実装」
 * ではなく「実装済みだが仮置きの定数」であり、値そのものは常に存在する
 * ため、`V5UnavailableMetric`の「— 未推定」という文言は誤り。CE CAGR・
 * RACRを表示する場所には必ずこれを添えること(呼び出し側の責務)。
 */
export function V5FailureFloorNote({ floor }: { floor?: number | null }) {
  if (floor == null) return null;
  const pct = (floor * 100).toFixed(0);
  return (
    <span
      className="v5-unavailable-metric v5-failure-floor-note"
      title={
        `破綻・非回収的上場廃止時に元本の${pct}%が回収されると仮定した保守的な定数です` +
        "(実測ではありません)。この定数だけを0.01→0.50に動かすと、母集団の中央値CE CAGR" +
        "は10ポイント以上変わります(docs/racr_shadow_run_diagnostic_2026-09-04.md §4)。" +
        "CE CAGR・RACRは絶対水準ではなく、銘柄間の相対順位として読んでください。"
      }
    >
      ※回収率{pct}%仮定
    </span>
  );
}
