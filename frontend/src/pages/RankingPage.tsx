import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { fetchCandidates } from "../api/client";
import type { CandidateListResponse } from "../api/types";
import { useCurrency } from "../currency";
import { CollectionStatusBanner } from "../components/CollectionStatusBanner";
import { TargetSelector, type TargetChoice } from "../components/TargetSelector";
import { Term } from "../components/Term";
import { WarningBadges } from "../components/WarningBadges";
import { V5RankingSection } from "../components/V5RankingSection";

const PAGE_SIZE = 50;
const DEFAULT_TARGET: TargetChoice = { horizonYears: 7, targetMoic: 10 };

function formatMarketCap(value: number | null): string {
  if (value == null) return "—";
  if (value >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  return `$${value.toLocaleString()}`;
}

/**
 * P(7年で10倍)の表示。14.2のとおり基準率は1%未満なので、パーセント表示は
 * 小数2桁まで出さないと上位と下位の区別がつかない。
 */
function formatProbability(p: number): string {
  const pct = p * 100;
  if (pct >= 1) return `${pct.toFixed(1)}%`;
  if (pct >= 0.01) return `${pct.toFixed(2)}%`;
  return `<0.01%`;
}

function probabilityTier(p: number): string {
  if (p >= 0.02) return "score-tier-high";
  if (p >= 0.002) return "score-tier-mid";
  return "score-tier-low";
}

function formatUsd(value: number | null): string {
  if (value == null) return "—";
  if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

/** 30.2.1:取扱可否バッジ。"unknown" を「不可」と誤読させない見た目にする。 */
function TradabilityBadge({ status }: { status: string }) {
  const label = status === "tradable" ? "取扱あり" : status === "not_listed" ? "対象外" : "未確認";
  const cls =
    status === "tradable"
      ? "tradability-badge tradability-tradable"
      : status === "not_listed"
        ? "tradability-badge tradability-not-listed"
        : "tradability-badge tradability-unknown";
  return (
    <span className={cls} title={status === "unknown" ? "取扱可否リストが未整備です(不可という意味ではありません)" : undefined}>
      {label}
    </span>
  );
}

export function RankingPage() {
  // 目標はURLに載せる。「3年で3倍のランキング」をそのまま共有・ブックマークでき、
  // ブラウザの戻るボタンが目標の切り替え履歴として機能する。
  const [searchParams, setSearchParams] = useSearchParams();
  const target: TargetChoice = {
    horizonYears: Number(searchParams.get("h") ?? DEFAULT_TARGET.horizonYears),
    targetMoic: Number(searchParams.get("m") ?? DEFAULT_TARGET.targetMoic),
  };
  const setTarget = (next: TargetChoice) => {
    const params = new URLSearchParams(searchParams);
    params.set("h", String(next.horizonYears));
    params.set("m", String(next.targetMoic));
    // 目標を変えると順位が総入れ替えになるので、ページングは先頭へ戻す。
    // これを「目標が変わったら offset を 0 にする」副作用(useEffect)でやっていた
    // ときは、**古い offset のまま新しい目標で1回フェッチしてから**改めて
    // offset=0 で取り直していた。無駄なリクエストが出るだけでなく、その一瞬だけ
    // 存在しないページ(例:全12件の目標で offset=100)の空リストが表示される。
    // 順位が変わる原因はこの操作そのものなので、ここで一緒に戻すのが正しい。
    setOffset(0);
    setSearchParams(params);
  };

  // Phase 8(Issue #3 §28・§29・§34・§36):v4(Champion)/v5(Shadow Challenger)
  // の切り替え。URLに載せるのは目標と同じ理由(共有・戻るボタンでの切り替え)。
  // 既定は v4 のまま——v5 は「あくまで切替先」であり、既存利用者の初回表示は
  // 一切変わらない。
  const model = searchParams.get("model") === "v5" ? "v5" : "v4";
  const setModel = (next: "v4" | "v5") => {
    const params = new URLSearchParams(searchParams);
    if (next === "v4") {
      params.delete("model");
    } else {
      params.set("model", next);
    }
    setSearchParams(params);
  };

  const [sector, setSector] = useState("");
  const [minMarketCap, setMinMarketCap] = useState("");
  const [maxMarketCap, setMaxMarketCap] = useState("");
  // 30.2.1:既定はfalse。取扱可否リストが未整備の利用者に空の画面を見せないため。
  const [tradableOnly, setTradableOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<CandidateListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { formatMoney } = useCurrency(); // J-10:円換算表示

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchCandidates({
      sector: sector || undefined,
      minMarketCap: minMarketCap ? Number(minMarketCap) : undefined,
      maxMarketCap: maxMarketCap ? Number(maxMarketCap) : undefined,
      horizonYears: target.horizonYears,
      targetMoic: target.targetMoic,
      tradableOnly,
      limit: PAGE_SIZE,
      offset,
    })
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [sector, minMarketCap, maxMarketCap, tradableOnly, offset, target.horizonYears, target.targetMoic]);

  return (
    <div>
      {/* Phase 8(Issue #3 §28):v4(Champion)/v5(Shadow Challenger)切替。
          既定は v4。v5選択時もこのページ自身のv4データ取得(useEffect群)は
          そのまま動き続けるが、描画するJSXだけを切り替えることで、既存の
          v4画面の表示(および挙動)を一切変えないという要件を守る。 */}
      <div className="model-toggle" role="group" aria-label="モデル選択">
        <button
          type="button"
          className={model === "v4" ? "active" : ""}
          aria-pressed={model === "v4"}
          onClick={() => setModel("v4")}
        >
          v4 Legacy(Champion)
        </button>
        <button
          type="button"
          className={model === "v5" ? "active" : ""}
          aria-pressed={model === "v5"}
          onClick={() => setModel("v5")}
        >
          v5 Shadow(Challenger)
        </button>
      </div>
      {model === "v5" ? (
        <V5RankingSection />
      ) : (
        <>
      {/* §6.5:バナーが出す「表示中のランキングは…時点」の日付は、この画面が
          実際に表示しているスコア確定日そのものでなければ意味が無い。別途
          APIから取らず、`GET /candidates` の応答をそのまま渡す。 */}
      <CollectionStatusBanner scoreDate={data?.score_date} />
      <h2>
        ランキング一覧
        {data?.target && (
          <span className="ranking-target">
            {data.target.horizon_years}年で{data.target.target_moic}倍
          </span>
        )}
      </h2>
      {data?.score_date && (
        <p className="score-date">
          スコア確定日: {data.score_date} ・ 対象 {data.total}銘柄
          {data.target && !data.target.is_default &&
            "(この目標で計算し直した結果です。1年オンペース率は既定の「7年で10倍」の実測でしか較正されていないため、ここでは表示されません)"}
        </p>
      )}

      {/* 28.18:用語が分からないまま順位だけを見る状態を作らない */}
      <div className="beginner-strip">
        <span className="beginner-strip-label">はじめての方へ</span>
        <span>
          「マルチプル」「希薄化」「デシル」など見慣れない言葉は、
          <strong>点線の下線にマウスを乗せる</strong>とその場で説明が出ます。
          全部まとめて読むなら<Link to="/glossary">用語集</Link>へ。
        </span>
      </div>

      {/* 14.2:「上位デシルでも大半は外れる前提をUI上にも明示すること」 */}
      <div className="model-notice">
        <strong>この確率は当たりの予告ではありません。</strong> 上位銘柄でも
        <em>大半は外れます</em>。順位は各銘柄の売上成長・利益率・<Term id="multiple">マルチプル</Term>・
        <Term id="dilution">希薄化</Term>・<Term id="survival">生存確率</Term>を
        目標年数まで外挿した推定にすぎません
        (<Link to="/validation">モデルの検証状況</Link>を必ず確認してください)。
        確率の<strong>絶対値</strong>は目標の選び方だけで大きく動くので、序列として読んでください。
      </div>

      {/* B-2/B-4/C-6(docs/model_audit_v4_2026-08-26.md):検証範囲・最悪日・買収リスクの明示 */}
      <div className="model-notice">
        <strong>検証は1年ホライズンでしか行われていません。</strong>
        {" "}目標が「7年で10倍」でも、実測(rank IC +0.15)は1年分の擬似バックテストです。
        7年後の実測はどこにも存在しません。さらに8評価日のうち1日は上位デシルがユニバースを
        <em>下回って</em>おり、常に効くモデルではありません。
        また、小型・割安な銘柄はTOB(買収)により目標到達前に上場廃止となることがあり、
        このモデルはそのシナリオを扱っていません。
      </div>

      <TargetSelector value={target} onChange={setTarget} effective={data?.target ?? null} />

      {data?.portfolio && data.portfolio.holdings > 0 && (
        <div className="portfolio-outlook">
          <h3>この{data.portfolio.holdings}銘柄をまとめて持ったら</h3>
          <div className="portfolio-figures">
            <div>
              <span className="portfolio-label">少なくとも1銘柄が目標到達</span>
              <strong>{(data.portfolio.probability_at_least_one * 100).toFixed(1)}%</strong>
            </div>
            <div>
              <span className="portfolio-label">2銘柄以上</span>
              <strong>{(data.portfolio.probability_at_least_two * 100).toFixed(1)}%</strong>
            </div>
            <div>
              <span className="portfolio-label">
                <Term id="expected-hits">期待本数</Term>
              </span>
              <strong>{data.portfolio.expected_hits.toFixed(2)}銘柄</strong>
            </div>
          </div>
          <p className="portfolio-note">
            <strong>銘柄どうしは独立ではありません。</strong>{" "}
            10倍銘柄の発生はマクロ環境・金利・セクター循環という共通因子に支配されており、
            当たる時期には多くの銘柄が同時に当たり、外れる時期にはどれも外れます。
            銘柄ごとの確率をそのまま掛け合わせる(独立を仮定する)と{" "}
            {(data.portfolio.probability_at_least_one_if_independent * 100).toFixed(1)}%
            になりますが、擬似バックテストから推定した<Term id="asset-correlation">資産相関</Term>{" "}
            {data.portfolio.asset_correlation.toFixed(3)} を織り込むと上の値まで下がります。
            <strong>N銘柄買ってもN回の独立な試行にはなりません。</strong>
          </p>
        </div>
      )}

      <div className="filters">
        <label>
          セクター
          <input
            value={sector}
            onChange={(e) => {
              setSector(e.target.value);
              setOffset(0);
            }}
            placeholder="例: Healthcare"
          />
        </label>
        <label>
          時価総額(下限, USD)
          <input
            type="number"
            value={minMarketCap}
            onChange={(e) => {
              setMinMarketCap(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <label>
          時価総額(上限, USD)
          <input
            type="number"
            value={maxMarketCap}
            onChange={(e) => {
              setMaxMarketCap(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <label className="checkbox-filter">
          <input
            type="checkbox"
            checked={tradableOnly}
            onChange={(e) => {
              setTradableOnly(e.target.checked);
              setOffset(0);
            }}
          />
          <Term id="tradability">取扱可</Term>の銘柄のみ
        </label>
      </div>

      {loading && <p>読み込み中...</p>}
      {error && <p className="error">エラー: {error}</p>}

      {data && data.items.length === 0 && !loading && <p>該当する候補がありません。</p>}

      {data && data.items.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="data-table ranking-table">
              <thead>
                <tr>
                  <th>順位</th>
                  <th>銘柄</th>
                  <th>
                    <Term id="sector">セクター</Term>
                  </th>
                  <th>
                    <Term id="market-cap">時価総額</Term>
                  </th>
                  <th>株価</th>
                  <th>
                    <Term id="probability-score">P({data.target?.target_moic ?? 10}倍)</Term>
                  </th>
                  <th>
                    <Term id="on-pace">1年オンペース率</Term>
                    <span className="th-badge">実測較正</span>
                    {/* B-3(docs/model_audit_v4_2026-08-26.md):較正は観測範囲の外へ外挿しない
                        ため、上位の帯では同じ値に飽和する(差が無い)ことがある */}
                  </th>
                  <th>
                    <Term id="expected-moic">期待倍率</Term>
                  </th>
                  <th>
                    <Term id="median-moic">中央値倍率</Term>
                  </th>
                  <th>
                    <Term id="survival">生存確率</Term>
                  </th>
                  {/* E-5(docs/defect_audit_2026-08-27.md):C-1が「一覧と詳細の両方に」
                      求めていた下振れ確率。一覧では最初に目に入る画面なので必須。 */}
                  <th>
                    <Term id="downside-probability">下振れ(半値以下)</Term>
                  </th>
                  <th>年率ボラ</th>
                  <th>最大DD(3年)</th>
                  {/* 30.2.1 / 30.2.2:取扱可否・投入上限列 */}
                  <th>
                    <Term id="tradability">取扱可否</Term>
                  </th>
                  <th>
                    <Term id="max_position">投入上限</Term>
                  </th>
                  <th>足場</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item, index) => {
                  // B-3:較正は観測範囲の外へ外挿しないため、上位の帯では複数銘柄が
                  // 同じ値に張り付く(=銘柄間の差が存在しない)。
                  // **前後どちらかと一致すれば飽和**とみなす。直前の行だけを見ると、
                  // ページの先頭行(offset>0でも index===0)が必ず判定から漏れ、
                  // 同じ値なのに1行だけバッジが付かない、という不整合が出る。
                  const isSaturated =
                    item.calibrated_on_pace_probability != null &&
                    (data.items[index - 1]?.calibrated_on_pace_probability ===
                      item.calibrated_on_pace_probability ||
                      data.items[index + 1]?.calibrated_on_pace_probability ===
                        item.calibrated_on_pace_probability);
                  return (
                    <tr key={item.ticker}>
                      <td className="rank-cell">{item.rank}</td>
                      <td>
                        <Link
                          to={`/candidates/${item.ticker}?h=${target.horizonYears}&m=${target.targetMoic}`}
                          className="ticker-link"
                        >
                          <span className="ticker-symbol">{item.ticker}</span>
                          {item.company_name && <span className="company-name-cell">{item.company_name}</span>}
                        </Link>
                      </td>
                      <td>{item.sector ?? "—"}</td>
                      <td>{item.market_cap != null ? formatMoney(item.market_cap) : formatMarketCap(item.market_cap)}</td>
                      <td>{item.price != null ? `$${item.price.toFixed(2)}` : "—"}</td>
                      <td>
                        <span className={`score-pill ${probabilityTier(item.probability)}`}>
                          {formatProbability(item.probability)}
                        </span>
                      </td>
                      <td>
                        {item.calibrated_on_pace_probability != null
                          ? `${(item.calibrated_on_pace_probability * 100).toFixed(1)}%`
                          : "—"}
                        {isSaturated && (
                          <span
                            className="th-badge"
                            title="較正が観測範囲の外へ外挿しないため、この帯では値が飽和しています(銘柄間の差はありません)"
                          >
                            飽和帯
                          </span>
                        )}
                      </td>
                      <td>
                        {item.expected_moic != null ? `${item.expected_moic.toFixed(1)}x` : "—"}
                        {item.moic_p10 != null && item.moic_p90 != null && (
                          <span className="detail-cagr">
                            {" "}
                            ({item.moic_p10.toFixed(1)} — {item.moic_p90.toFixed(1)})
                          </span>
                        )}
                      </td>
                      <td>{item.median_moic != null ? `${item.median_moic.toFixed(1)}x` : "—"}</td>
                      <td>
                        {item.survival_probability != null
                          ? `${(item.survival_probability * 100).toFixed(0)}%`
                          : "—"}
                      </td>
                      <td>
                        {item.probability_below_half != null
                          ? `${(item.probability_below_half * 100).toFixed(1)}%`
                          : "—"}
                      </td>
                      <td>{item.realized_vol_1y != null ? `${(item.realized_vol_1y * 100).toFixed(0)}%` : "観測不足"}</td>
                      <td>{item.max_drawdown_3y != null ? `${(item.max_drawdown_3y * 100).toFixed(0)}%` : "観測不足"}</td>
                      <td>
                        <TradabilityBadge status={item.tradability} />
                        {item.blocking_flag_count > 0 && (
                          <span className="red-flag-badge red-flag-blocking" title="即死要因(BLOCKING)あり">
                            ⚠ {item.blocking_flag_count}
                          </span>
                        )}
                      </td>
                      <td>
                        {formatUsd(item.max_position_usd)}
                        {item.position_binding_constraint && (
                          <span className="th-badge" title="どちらの制約が効いているか">
                            {item.position_binding_constraint === "liquidity" ? "流動性" : "規律"}
                          </span>
                        )}
                      </td>
                      <td>
                        <span title={item.evidence_grade?.reasons.join(" / ")} className="th-badge">{item.evidence_grade?.grade ?? "—"}</span>
                        <WarningBadges codes={item.warnings} compact />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              前へ
            </button>
            <span>
              {offset + 1}〜{Math.min(offset + PAGE_SIZE, data.total)} / {data.total}件
            </span>
            <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              次へ
            </button>
          </div>
        </>
      )}
        </>
      )}
    </div>
  );
}
