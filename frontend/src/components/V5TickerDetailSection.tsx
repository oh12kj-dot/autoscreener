import { useEffect, useState } from "react";
import { fetchCandidates, fetchV5Objectives, fetchV5ScoreDetail } from "../api/client";
import type { ModelV5AblationEntry, ModelV5ObjectivesResponse, ModelV5ScoreDetail } from "../api/types";
import { V5WarningBadges } from "./V5WarningBadges";
import {
  v5AblationReasonLabel,
  v5ObjectiveLabel,
  v5SignalLabel,
  v5StateShiftLabel,
} from "../v5Labels";

/** Phase 8(Issue #3 §28・§29・§34・§36):TickerDetailPage向けのv5専用セクション。
 *  v4の描画コードとは完全に分離した、追加専用(additive-only)のコンポーネント
 *  ——既存v4画面のJSX・フックには一切触れない。
 *
 *  Phase 11(2026-09-03「v5のUIが見れたものではない」指摘への対応):
 *  ablationの行キーは signal key(guidance/litigation/accounting_quality等)
 *  であり、state_shift の内訳キー(growth_duration_years等)とは別の名前
 *  空間。以前はこの2つを同じ `label()` で解決していたため、外側の行見出し
 *  は常に生のsignal keyのまま出ていた(FEATURE_LABELSがstate_shift用の
 *  キーしか知らなかったため)。`v5Labels.ts` で両方を別関数に分離する。 */

function fmtYears(v: number): string {
  return `${v.toFixed(1)}y`;
}

function fmtRate(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

function fmtDelta(v: number, digits = 3): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
}

function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  const p = v * 100;
  if (p >= 1) return `${p.toFixed(1)}%`;
  if (p >= 0.01) return `${p.toFixed(2)}%`;
  return `<0.01%`;
}

/** state_shift の各キーは単位がバラバラ(年・率・比率・倍率)なので、
 *  「+5点」のような無意味な点数化はせず、キーごとに意味のある単位で
 *  「without → with (Δ)」の形式に直す。`states`(現在値=with)から逆算できる
 *  キー(成長期間・初期成長率)だけ実値の推移を示し、それ以外は
 *  Δ(差分)のみを正直に示す——存在しないデータをでっち上げない。 */
function renderStateShift(
  shift: Record<string, number>,
  states: Record<string, unknown>
): { key: string; text: string }[] {
  const growth = (states as { growth?: Record<string, { value?: number | null }> }).growth;
  const rows: { key: string; text: string }[] = [];
  for (const [key, delta] of Object.entries(shift)) {
    if (key === "growth_duration_years" && growth?.duration_years?.value != null) {
      const withV = growth.duration_years.value;
      const withoutV = withV - delta;
      rows.push({ key, text: `${v5StateShiftLabel(key)}: ${fmtYears(withoutV)} → ${fmtYears(withV)} (Δ${fmtDelta(delta, 2)}y)` });
    } else if (key === "initial_growth_rate" && growth?.initial_rate?.value != null) {
      const withV = growth.initial_rate.value;
      const withoutV = withV - delta;
      rows.push({ key, text: `${v5StateShiftLabel(key)}: ${fmtRate(withoutV)} → ${fmtRate(withV)} (Δ${fmtDelta(delta * 100, 1)}pt)` });
    } else {
      rows.push({ key, text: `${v5StateShiftLabel(key)}: Δ${fmtDelta(delta)}` });
    }
  }
  return rows;
}

interface Props {
  ticker: string;
  v4Probability: number | null;
  v4ExpectedMoic: number | null;
}

export function V5TickerDetailSection({ ticker, v4Probability, v4ExpectedMoic }: Props) {
  const [detail, setDetail] = useState<ModelV5ScoreDetail | null>(null);
  const [objectives, setObjectives] = useState<ModelV5ObjectivesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [v4Rank, setV4Rank] = useState<number | "not_ranked" | null>(null);
  const [v4RankLoading, setV4RankLoading] = useState(false);

  useEffect(() => {
    setDetail(null);
    setError(null);
    Promise.all([fetchV5ScoreDetail(ticker), fetchV5Objectives()])
      .then(([d, o]) => {
        setDetail(d);
        setObjectives(o);
      })
      .catch((e: Error) => setError(e.message));
  }, [ticker]);

  // /candidates の limit は最大200(_MAX_LIMIT、routes.py)。411銘柄などの
  // 母集団を1回で取得しようとすると 422 Unprocessable Entity になる
  // (Phase 11の実機確認で発見した実バグ)。200件ずつページングして総当たりする。
  const V4_CANDIDATES_PAGE_LIMIT = 200;

  const loadV4Rank = async () => {
    setV4RankLoading(true);
    try {
      const first = await fetchCandidates({ limit: 1, offset: 0 });
      let foundIndex = -1;
      for (let offset = 0; offset < first.total; offset += V4_CANDIDATES_PAGE_LIMIT) {
        const page = await fetchCandidates({ limit: V4_CANDIDATES_PAGE_LIMIT, offset });
        const idxInPage = page.items.findIndex((item) => item.ticker === ticker);
        if (idxInPage !== -1) {
          foundIndex = offset + idxInPage;
          break;
        }
      }
      setV4Rank(foundIndex === -1 ? "not_ranked" : foundIndex + 1);
    } catch {
      setV4Rank("not_ranked");
    } finally {
      setV4RankLoading(false);
    }
  };

  if (error) {
    return (
      <div className="v5-ticker-section">
        <h3>v5 Shadow Challenger</h3>
        <p className="v5-not-available">v5スコアは未取得です({error})。</p>
      </div>
    );
  }
  if (!detail || !objectives) {
    return (
      <div className="v5-ticker-section">
        <h3>v5 Shadow Challenger</h3>
        <p>読み込み中…</p>
      </div>
    );
  }

  const defaultObjective = objectives.default_objective;
  const v5ObjectiveScore = detail.objectives.find((o) => o.objective === defaultObjective) ?? null;
  const ablationEntries = Object.entries(detail.features.ablation ?? {}) as [string, ModelV5AblationEntry][];

  return (
    <div className="v5-ticker-section">
      <h3>v5 Shadow Challenger(参考・投資判断には未使用)</h3>
      <div className="v5-badges">
        <span className="v5-badge" title="forward_shadow_only">将来検証のみ</span>
        <span className="v5-badge" title="not_for_production">投資判断には未使用</span>
        <span className="v5-badge" title="historical_backtest_supported=false">
          一部特徴量は過去再現(historical backtest)未対応
        </span>
      </div>
      <p className="v5-caveat">
        v5はChampion(v4)と並行して動く検証専用モデルです。実現リターンによる検証(forward
        validation)はまだ成熟していません(詳細は<a href="/validation">検証状況</a>を参照)。
      </p>

      <table className="v5-compare-table">
        <thead>
          <tr>
            <th></th>
            <th>v4(Champion)</th>
            <th>v5(Challenger)</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>P(目標倍率到達)</td>
            <td>{pct(v4Probability)}</td>
            <td>{pct(detail.distribution.p_target)}</td>
          </tr>
          <tr>
            <td>期待MOIC</td>
            <td>{v4ExpectedMoic != null ? `${v4ExpectedMoic.toFixed(2)}x` : "—"}</td>
            <td>{detail.distribution.expected_moic != null ? `${detail.distribution.expected_moic.toFixed(2)}x` : "—"}</td>
          </tr>
          <tr>
            <td>{v5ObjectiveLabel(defaultObjective)}での順位</td>
            <td>
              {v4Rank == null ? (
                <button type="button" disabled={v4RankLoading} onClick={loadV4Rank}>
                  {v4RankLoading ? "計算中…" : "計算する"}
                </button>
              ) : v4Rank === "not_ranked" ? (
                "順位なし"
              ) : (
                `${v4Rank}位`
              )}
            </td>
            <td>{v5ObjectiveScore?.rank != null ? `${v5ObjectiveScore.rank}位` : "未計算"}</td>
          </tr>
        </tbody>
      </table>

      <h4>なぜこの分布になったか(特徴量ごとの寄与)</h4>
      <p className="v5-caveat">
        各行は「その特徴量を除いたら(without)どうなっていたか」との差分(leave-one-out
        ablation)。coverage gate で無効化された特徴量は理由付きで「未計算」と表示します。
      </p>
      <table className="v5-ablation-table">
        <thead>
          <tr>
            <th>特徴量</th>
            <th>状態</th>
            <th>詳細</th>
          </tr>
        </thead>
        <tbody>
          {ablationEntries.length === 0 && (
            <tr>
              <td colSpan={3}>特徴量データなし</td>
            </tr>
          )}
          {ablationEntries.map(([key, entry]) => (
            <tr key={key}>
              <td>{v5SignalLabel(key)}</td>
              <td>{entry.status === "computed" ? "計算済み" : "未計算"}</td>
              <td>
                {entry.status === "computed" && entry.state_shift ? (
                  <ul className="v5-state-shift-list">
                    {renderStateShift(entry.state_shift, detail.states).map((row) => (
                      <li key={row.key}>{row.text}</li>
                    ))}
                  </ul>
                ) : (
                  <span className="v5-not-computed-reason">
                    {v5AblationReasonLabel(entry.reason)}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail.warnings.length > 0 && (
        <div className="v5-warnings">
          <V5WarningBadges codes={detail.warnings} compact />
        </div>
      )}
      <p className="v5-confidence">モデル信頼度: {(detail.confidence * 100).toFixed(0)}%</p>
    </div>
  );
}
