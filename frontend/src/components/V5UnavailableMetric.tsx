import { v5UnavailableReasonLabel } from "../v5Labels";

/**
 * WP-C(docs/racr_wp_c_api_ui_2026-09-04.md)の中心要件専用コンポーネント:
 * `p_permanent_loss` / `expected_max_drawdown` / `p_mdd_above_30/50/70` /
 * `recovery_time_median` は現行モデルでは常に `null`(未実装)であり、
 * **0%や空欄のセルとして表示してはならない**——利用者が「この銘柄は
 * 永久損失リスクが無い」と誤読する、この一連のUI作業そのものの理由になった
 * 誤読を再現しないため。
 *
 * 常に固定文言「— 未推定」を表示し、理由は破棄しない(title属性=ネイティブ
 * ツールチップとして常に併記する)。呼び出し側は値がnullのときにだけこれを
 * 使うこと——値がある場合にこのコンポーネントへ迷い込ませない。
 */
export function V5UnavailableMetric({ reason }: { reason?: string | null }) {
  return (
    <span className="v5-unavailable-metric" title={v5UnavailableReasonLabel(reason)}>
      — 未推定
    </span>
  );
}
