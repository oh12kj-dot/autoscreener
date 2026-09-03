import { useEffect, useState } from "react";
import { fetchV5ValidationStatus } from "../api/client";
import type { ModelV5ValidationStatus } from "../api/types";
import { V5WarningBadges } from "./V5WarningBadges";
import { v5DecisionLabel, v5ModeLabel, v5RunStatusLabel, v5SignalLabel } from "../v5Labels";

/** Phase 8/9(Issue #3 §28・§29・§34・§36):v5(Shadow Challenger)の検証状況。
 *  v4の検証セクションとは別枠の追加専用コンポーネント。数字は
 *  `/api/v1/models/v5/validation-status` から実測値をそのまま表示する
 *  ——ハードコードしない。データが無い箇所は「データ不足」と明示し、
 *  空欄や0と誤読させない(Phase 7実測:評価日9件・realized return 0件)。 */
export function V5ValidationSection() {
  const [status, setStatus] = useState<ModelV5ValidationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchV5ValidationStatus()
      .then(setStatus)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="v5-validation-section">
        <h3>v5 Shadow Challenger の検証状況</h3>
        <p className="v5-not-available">取得に失敗しました({error})。</p>
      </div>
    );
  }
  if (!status) {
    return (
      <div className="v5-validation-section">
        <h3>v5 Shadow Challenger の検証状況</h3>
        <p>読み込み中…</p>
      </div>
    );
  }

  const hasRealizedOutcomes = status.realized_forward_validation_count > 0;

  return (
    <div className="v5-validation-section">
      <h3>v5 Shadow Challenger の検証状況</h3>
      <div className="v5-badges">
        <span className="v5-badge" title="forward_shadow_only">将来検証のみ</span>
        <span className="v5-badge" title="not_for_production">投資判断には未使用</span>
      </div>
      <div className="model-notice negative">
        <strong>
          v5はまだ投資判断に使える品質ではありません(historical_backtest_supported=false
          の特徴量あり)。
        </strong>
        <p>
          v4(Champion)はこのページ上部の実測バックテストで検証されていますが、v5(Challenger)は
          <strong>実現リターンによる検証がまだ成熟していません</strong>。理由は下記の通りです。
        </p>
      </div>

      <table className="v5-validation-table">
        <tbody>
          <tr>
            <td>Champion / Challenger</td>
            <td>
              {status.champion_model} / {status.challenger_model}（{v5ModeLabel(status.challenger_mode)}）
            </td>
          </tr>
          <tr>
            <td>昇格判断(Decision Record)</td>
            <td>
              {v5DecisionLabel(status.decision)}
              {status.decision_entry_date && `(${status.decision_entry_date}時点)`}
            </td>
          </tr>
          <tr>
            <td>最終run</td>
            <td>
              {status.latest_run ? (
                <>
                  {status.latest_run.as_of}（状態: {v5RunStatusLabel(status.latest_run.status)}、config_hash:{" "}
                  {status.latest_run.config_hash ?? "—"}）
                </>
              ) : (
                "データ不足(run記録なし)"
              )}
            </td>
          </tr>
          <tr>
            <td>PIT評価対象日数</td>
            <td>
              {status.evaluation_dates_count > 0 ? (
                <>
                  {status.evaluation_dates_count}日
                  {status.evaluation_date_range &&
                    `(${status.evaluation_date_range[0]} 〜 ${status.evaluation_date_range[1]})`}
                </>
              ) : (
                "データ不足(0日)"
              )}
            </td>
          </tr>
          <tr>
            <td>実現リターンによる forward validation</td>
            <td>
              {hasRealizedOutcomes ? (
                `${status.realized_forward_validation_count}件`
              ) : (
                <span className="v5-insufficient-data">
                  データ不足(実現済み観測 0件・INSUFFICIENT_DATA) — 評価対象期間がまだ短く、
                  target_horizon_years に到達した銘柄がありません。0件を「効果なし」と読まず、
                  「まだ測れない」と読んでください。
                </span>
              )}
            </td>
          </tr>
          <tr>
            <td>既知の未対応特徴量(historical_backtest_supported=false)</td>
            <td>
              {status.unsupported_historical_features.length > 0
                ? status.unsupported_historical_features.map((k) => v5SignalLabel(k)).join("、")
                : "なし"}
            </td>
          </tr>
        </tbody>
      </table>

      {status.warnings.length > 0 && (
        <div className="v5-warnings">
          <V5WarningBadges codes={status.warnings} compact />
        </div>
      )}
    </div>
  );
}
