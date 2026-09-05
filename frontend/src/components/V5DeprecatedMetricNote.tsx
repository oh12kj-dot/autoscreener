/**
 * Defect 3(2026-09-05監査、docs/audit_followup_2026-09-05.md;縮退の実測は
 * docs/racr_shadow_run_diagnostic_2026-09-04.md §3.1):
 * `expected_shortfall_10pct_log` は固定10%分位点で計算されており、実在する
 * どの銘柄でも破綻確率atomが既にその分位を超えるため、**全銘柄で完全に
 * 同一の定数**(実測: -0.657881455)になる——銘柄間の順位付けやリスク情報を
 * 一切持たない。後方互換のためだけにAPIへ残しており、生きたリスク指標として
 * 読んではならない。
 *
 * `V5FailureFloorNote`/`V5UnavailableMetric` と同じ「隠さない」方針を
 * 踏襲する専用コンポーネント——値そのものは表示しつつ、非推奨・定数である
 * ことを常に併記する。呼び出し側はこのフィールドを表示する箇所に必ず
 * 添えること。
 */
export function V5DeprecatedMetricNote() {
  return (
    <span
      className="v5-unavailable-metric v5-deprecated-metric-note"
      title={
        "非推奨: 固定10%分位点で計算されるため、実在するほぼ全ての銘柄で" +
        "破綻確率が既にその分位を超え、全銘柄が完全に同一の定数になります" +
        "(生きたリスク指標ではありません)。後方互換のためだけにAPIへ残して" +
        "います。銘柄間の比較には expected_shortfall_10pct_log_given_survival" +
        "(下位10%期待損失・生存条件付)を使ってください。"
      }
    >
      ※非推奨・全銘柄同一定数
    </span>
  );
}
