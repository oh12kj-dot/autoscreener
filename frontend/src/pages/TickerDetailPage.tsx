import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, fetchCandidateDetail } from "../api/client";
import type { CandidateDetail } from "../api/types";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useCurrency } from "../currency";
import { DueDiligenceChecklist } from "../components/DueDiligenceChecklist";
import { FinancialHistorySection } from "../components/FinancialHistorySection";
import { FilingsTimelineSection } from "../components/FilingsTimelineSection";
import { PeerComparisonSection } from "../components/PeerComparisonSection";
import { BenchmarkReferenceSection } from "../components/BenchmarkReferenceSection";
import { LlmAnalysisSection } from "../components/LlmAnalysisSection";
import { ScoreHistoryChart } from "../components/ScoreHistoryChart";
import { Term } from "../components/Term";
import { WarningBadges } from "../components/WarningBadges";
import { InvestmentIntelligenceSections } from "../components/InvestmentIntelligenceSections";
import { V5TickerDetailSection } from "../components/V5TickerDetailSection";
import type { GlossaryId } from "../glossary";

/** `factors` に入っている診断値の表示。内訳5因子とは別枠で並べる。
 *  `term` を持つ項目は用語集(28.18)へのツールチップが付く。 */
const DIAGNOSTICS: { key: string; label: string; term?: GlossaryId; format: (v: number) => string }[] = [
  {
    key: "base_growth_rate",
    label: "初期成長率(財務諸表のみ)",
    format: (v) => `${(v * 100).toFixed(1)}%`,
  },
  {
    key: "growth_nowcast_adjustment",
    label: "株価トレンドによる補正",
    term: "nowcast",
    format: (v) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}pt`,
  },
  { key: "growth_cyclicality_adjustment", label: "循環性による成長率の割引", format: (v) => `${(v * 100).toFixed(1)}pt` },
  { key: "initial_growth_rate", label: "初期成長率(補正後・減衰前)", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "terminal_growth_rate", label: "目標年数後の成長率", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "growth_fade_rate", label: "成長の減衰率(質が高いほど大)", term: "growth-fade", format: (v) => v.toFixed(3) },
  { key: "terminal_gross_margin", label: "目標年数後の粗利率(推定)", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "current_ev_to_gross_profit", label: "現在の EV/粗利", term: "multiple", format: (v) => `${v.toFixed(1)}x` },
  { key: "target_ev_to_gross_profit", label: "目標年数後の EV/粗利(推定)", format: (v) => `${v.toFixed(1)}x` },
  {
    key: "implied_terminal_ev",
    label: "目標年数後の事業価値(推定)",
    format: (v) => (v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : `$${(v / 1e6).toFixed(0)}M`),
  },
  { key: "health_index", label: "財務健全性指標(−1〜+1)", term: "piotroski", format: (v) => v.toFixed(2) },
  { key: "size_prior", label: "規模の事前分布による補正", format: (v) => `${v.toFixed(2)}x` },
  {
    key: "lease_share_of_net_debt",
    label: "ネットデットに占めるリース債務の割合(S-5診断)",
    format: (v) => `${(v * 100).toFixed(0)}%`,
  },
];

/** J-3:`factors` から数値を安全に取り出す(文字列メタ情報が同居するため)。 */
function factorNumber(factors: Record<string, number | string> | null | undefined, key: string): number | null {
  const v = factors?.[key];
  return typeof v === "number" ? v : null;
}

const PERCENTILE_ROWS: { key: string; label: string }[] = [
  { key: "ev_to_gross_profit_percentile_universe", label: "EV/粗利(ユニバース内)" },
  { key: "ev_to_gross_profit_percentile_sector", label: "EV/粗利(セクター内)" },
  { key: "revenue_growth_percentile_sector", label: "売上成長(セクター内)" },
  { key: "gross_margin_percentile_sector", label: "粗利率(セクター内)" },
];

function formatProbability(p: number): string {
  const pct = p * 100;
  if (pct >= 1) return `${pct.toFixed(2)}%`;
  if (pct >= 0.01) return `${pct.toFixed(3)}%`;
  return `<0.01%`;
}

export function TickerDetailPage() {
  const { ticker } = useParams<{ ticker: string }>();
  // 一覧で選んだ目標(何年で何倍)を引き継ぐ。引き継がないと、一覧では
  // 「3年で3倍」で見ていたのに詳細だけ既定の7年になり、数字が食い違う。
  const [searchParams] = useSearchParams();
  const horizonYears = searchParams.get("h") ? Number(searchParams.get("h")) : undefined;
  const targetMoic = searchParams.get("m") ? Number(searchParams.get("m")) : undefined;
  // WP-C(docs/racr_wp_c_api_ui_2026-09-04.md):v5ランキングで選んだobjective
  // をURL経由でここへ引き継ぐ。無指定ならV5TickerDetailSection側がAPIの
  // default_objectiveへ落ち着く(不変条件3:ここでも既定をハードコードしない)。
  const v5Objective = searchParams.get("objective") || undefined;
  const v5AsOf = searchParams.get("as_of") || undefined;
  const [detail, setDetail] = useState<CandidateDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // J-1:事業概要は長文なので既定で折り畳む。
  const [summaryExpanded, setSummaryExpanded] = useState(false);
  // J-10:円換算表示。
  const { formatMoney } = useCurrency();

  useEffect(() => {
    if (!ticker) return;
    setLoading(true);
    setError(null);
    fetchCandidateDetail(ticker, { horizonYears, targetMoic })
      .then(setDetail)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) {
          setError(`銘柄 "${ticker}" は追跡対象のユニバースに存在しません。`);
        } else {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => setLoading(false));
  }, [ticker, horizonYears, targetMoic]);

  if (loading) return <p>読み込み中...</p>;
  if (error) return <p className="error">{error}</p>;
  if (!detail) return null;

  return (
    <div>
      <p>
        <Link to={`/?${searchParams.toString()}`}>← ランキングに戻る</Link>
      </p>
      <h2>
        {detail.ticker}
        {detail.company_name && <span className="company-name"> {detail.company_name}</span>}
      </h2>
      <p className="ticker-meta">
        {detail.sector ?? "セクター不明"}
        {detail.market_cap != null && ` ・ 時価総額 ${formatMoney(detail.market_cap)}`}
        {detail.price != null && ` ・ 株価 $${detail.price.toFixed(2)}`}
        {detail.next_event && (
          <span className="th-badge">
            {detail.next_event.event_type === "verification" ? "検証日" : "決算"}まで{" "}
            {detail.next_event.days_until}日({detail.next_event.event_date}
            {detail.next_event.is_estimated ? "・推定" : ""})
          </span>
        )}
      </p>

      {/* J-1:この会社は何をしているか。原文のまま。要約・翻訳は生成しない。 */}
      {detail.profile && (
        <div className="dd-section company-profile">
          <h3>この会社は何をしているか</h3>
          {detail.profile.business_summary ? (
            <>
              <p
                style={
                  summaryExpanded
                    ? undefined
                    : {
                        display: "-webkit-box",
                        WebkitLineClamp: 3,
                        WebkitBoxOrient: "vertical",
                        overflow: "hidden",
                      }
                }
              >
                {detail.profile.business_summary}
              </p>
              <button type="button" className="link-button" onClick={() => setSummaryExpanded((v) => !v)}>
                {summaryExpanded ? "折り畳む" : "全文"}
              </button>
            </>
          ) : (
            <p className="detail-cagr">事業概要は未取得です(この銘柄の info に longBusinessSummary がありません)。</p>
          )}
          <p className="ticker-meta">
            {detail.profile.industry && `業種: ${detail.profile.industry}`}
            {detail.profile.country && ` ・ 所在国: ${detail.profile.country}`}
            {detail.profile.full_time_employees != null &&
              ` ・ 従業員 ${detail.profile.full_time_employees.toLocaleString()}名`}
            {detail.profile.exchange && ` ・ 上場市場: ${detail.profile.exchange}`}
            {detail.profile.listed_date && ` ・ 上場 ${detail.profile.listed_date}`}
          </p>
          <p className="ticker-meta">
            {detail.profile.website && (
              <a href={detail.profile.website} target="_blank" rel="noreferrer">
                IRサイト
              </a>
            )}
            {detail.profile.cik ? (
              <>
                {detail.profile.website && " ・ "}
                <a
                  href={`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${detail.profile.cik}&type=10-K`}
                  target="_blank"
                  rel="noreferrer"
                >
                  EDGAR(10-K)
                </a>
              </>
            ) : (
              <span className="detail-cagr"> ・ CIK 未解決(refresh-cik-map を実行)</span>
            )}
          </p>
          {detail.profile.profile_as_of && (
            <p className="detail-cagr">この記述は {detail.profile.profile_as_of} 時点の info によるものです。</p>
          )}
          <p>保有構成: インサイダー {detail.profile.held_percent_insiders != null ? `${(detail.profile.held_percent_insiders * 100).toFixed(1)}%` : "—"} ・ 機関投資家 {detail.profile.held_percent_institutions != null ? `${(detail.profile.held_percent_institutions * 100).toFixed(1)}%` : "—"} ・ 浮動株比率 {detail.profile.float_ratio != null ? `${(detail.profile.float_ratio * 100).toFixed(1)}%` : "—"}</p>
          {detail.profile.officers.length > 0 && <p className="detail-cagr">経営陣: {detail.profile.officers.map((o) => `${o.name}${o.title ? ` (${o.title})` : ""}`).join(" / ")}</p>}
        </div>
      )}

      {!detail.is_candidate && (
        <div className="exclusion-banner">
          <strong>現在の候補プールには含まれていません。</strong>
          {detail.exclusion_reason && <p>除外理由: {detail.exclusion_reason.join(", ")}</p>}
        </div>
      )}

      {detail.unranked_reason === "negative_outlook" && (
        <div className="exclusion-banner">
          <strong>順位を付けていません(中心的な見通しがマイナス)。</strong>
          <p>
            データは揃っており計算もできましたが、売上成長・利益率・
            <Term id="multiple">マルチプル</Term>・<Term id="dilution">希薄化</Term>を
            外挿した<Term id="expected-moic">期待倍率</Term>が 1.0 を下回りました
            {detail.expected_moic != null && `(${detail.expected_moic.toFixed(2)}倍)`}。
            つまり<strong>中心的な見通しでは株主価値が減る</strong>ということです。
            <br />
            これは「データが無くて分からない」のとは違います。
            <em>測った結果がこうだった</em>という情報なので、下の内訳はそのまま読めます。
            事業が回復すれば候補に戻ります。
          </p>
        </div>
      )}

      {detail.probability == null && detail.unranked_reason == null && detail.target && !detail.target.is_default && (
        <div className="exclusion-banner">
          <strong>
            「{detail.target.horizon_years}年で{detail.target.target_moic}倍」ではランキング対象になりません。
          </strong>
          <p>
            この年数では複利が十分に効かず、期待倍率が1.0を下回ります(中心的な見通しで
            株主価値を毀損する)。年数を伸ばすと対象に戻る場合があります。
          </p>
        </div>
      )}

      {detail.probability != null && (
        <div className="score-summary">
          <div className="overall-score">{formatProbability(detail.probability)}</div>
          <div>
            <p>
              <strong>
                P({detail.target?.horizon_years ?? 7}年で{detail.target?.target_moic ?? 10}倍)
              </strong>
              {detail.target && (
                <span className="detail-cagr">
                  {" "}
                  必要年率 {(detail.target.required_cagr * 100).toFixed(1)}%
                </span>
              )}
            </p>
            <p>
              <Term id="expected-moic">期待倍率</Term> {detail.expected_moic?.toFixed(2) ?? "—"}x ・{" "}
              <Term id="median-moic">中央値倍率</Term> {detail.median_moic?.toFixed(2) ?? "—"}x
            </p>
            <p>
              <Term id="survival">生存確率</Term>{" "}
              {detail.survival_probability != null
                ? `${(detail.survival_probability * 100).toFixed(0)}%`
                : "—"}{" "}
              ・ <Term id="sigma">ばらつき(σ)</Term> {detail.log_moic_sigma?.toFixed(2) ?? "—"}
            </p>
            {(detail.probability_below_half != null || detail.probability_below_one != null) && (
              <p className="downside-line">
                <Term id="downside-probability">下振れ</Term>:P(半値以下) {detail.probability_below_half != null ? `${(detail.probability_below_half * 100).toFixed(1)}%` : "—"}
                {" "}・ P(元本割れ) {detail.probability_below_one != null ? `${(detail.probability_below_one * 100).toFixed(1)}%` : "—"}
              </p>
            )}
            {detail.calibrated_on_pace_probability != null && (
              <p className="calibrated-line">
                <strong>
                  <Term id="on-pace">1年オンペース率</Term>{" "}
                  {(detail.calibrated_on_pace_probability * 100).toFixed(1)}%
                </strong>{" "}
                <span className="th-badge">実測較正</span>{" "}
                <span className="detail-cagr">
                  今後1年で年率38.9%(=10倍/7年と同じペース)に乗る確率。
                  上のP(10倍)と違い、擬似バックテストの実測頻度で較正されており、
                  1年後に答え合わせできます
                </span>
              </p>
            )}
            <p>
              モデルバージョン: {detail.scoring_version ?? "—"} ・ 最終更新:{" "}
              {detail.last_updated ? new Date(detail.last_updated).toLocaleString("ja-JP") : "—"}
            </p>
            {/* C-6(docs/model_audit_v4_2026-08-26.md):買収シナリオはモデルの対象外 */}
            <p className="detail-cagr">
              小型・割安な銘柄はTOB(買収)で株価が現金決済され、目標倍率に届く前に上場廃止に
              なることがあります。このモデルはそのシナリオを扱っていません。
            </p>
          </div>
        </div>
      )}

      <WarningBadges codes={detail.warnings} />
      <p className="detail-cagr">表示している倍率・確率は米ドル建て・税引前・取引コスト控除前です。税・為替・為替手数料はこのアプリでは計算しません。</p>

      <InvestmentIntelligenceSections
        ticker={detail.ticker}
        horizonYears={detail.target?.horizon_years ?? 7}
        expectedMoic={detail.expected_moic}
        realizedVol={detail.price_risk?.realized_vol_1y ?? null}
        evidenceGrade={detail.evidence_grade?.grade ?? null}
      />

      {detail.evidence_grade && (
        <div className="dd-section"><h3>推定の足場: {detail.evidence_grade.grade}</h3>
          <p className="detail-cagr">これは会社の良し悪しではなく、推定を支えるデータ量と整合性の要約です。</p>
          {detail.evidence_grade.reasons.map((reason) => <p key={reason}>{reason}</p>)}
        </div>
      )}

      {/* J-4:実現倍率の分位点(生存確率込みの混合分布) */}
      {detail.moic_quantiles && (
        <div className="dd-section">
          <h3>実現倍率の幅(P10 — P50 — P90)</h3>
          {(() => {
            const q = detail.moic_quantiles!;
            const target = detail.target?.target_moic ?? 10;
            const hi = Math.max(q.p90 ?? 0, target) * 1.05 || 1;
            const pos = (v: number) => `${Math.max(0, Math.min(100, (v / hi) * 100)).toFixed(1)}%`;
            return (
              <>
                <div className="moic-range-track">
                  <div
                    className="moic-range-fill"
                    style={{ left: pos(q.p10 ?? 0), right: `calc(100% - ${pos(q.p90 ?? 0)})` }}
                  />
                  <div className="moic-range-tick" style={{ left: pos(q.p50 ?? 0) }} title={`P50 ${(q.p50 ?? 0).toFixed(2)}x`} />
                  <div className="moic-range-target" style={{ left: pos(target) }} title={`目標 ${target}x`} />
                </div>
                <p>
                  P10 {(q.p10 ?? 0).toFixed(2)}x ・ P25 {(q.p25 ?? 0).toFixed(2)}x ・ P50{" "}
                  {(q.p50 ?? 0).toFixed(2)}x ・ P75 {(q.p75 ?? 0).toFixed(2)}x ・ P90{" "}
                  {(q.p90 ?? 0).toFixed(2)}x（縦線=目標 {target}x）
                </p>
              </>
            );
          })()}
          <p className="detail-cagr">
            この幅は<strong>モデルの仮定によるもので、実測で較正されていません</strong>
            (較正は閾値超過確率にしか掛かっておらず、分位点には適用できません)。
            また σ の縮小(sigma_shrinkage 0.85)により銘柄差は 15% しか残らないため、
            <strong>幅はほぼ全銘柄で似た形になります</strong>——銘柄ごとにリスクを測れているわけではありません。
            P10 が 0.00x なのは、生存確率 {detail.survival_probability != null ? `${(detail.survival_probability * 100).toFixed(0)}%` : "—"} で
            倒産・上場廃止(実現倍率≈0)を混合分布に織り込んでいるためです。
          </p>
          <BenchmarkReferenceSection horizonYears={detail.target?.horizon_years ?? 7} />
        </div>
      )}

      {detail.price_risk && (
        <div className="dd-section">
          <h3>実測された値動き</h3>
          <p>観測 {detail.price_risk.observation_days} 日 ・ 年率ボラ {detail.price_risk.realized_vol_1y != null ? `${(detail.price_risk.realized_vol_1y * 100).toFixed(1)}%` : "観測不足"} ・ 最大DD(3年) {detail.price_risk.max_drawdown_3y != null ? `${(detail.price_risk.max_drawdown_3y * 100).toFixed(1)}%` : "観測不足"}</p>
          <p>現在のDD {detail.price_risk.currently_in_drawdown != null ? `${(detail.price_risk.currently_in_drawdown * 100).toFixed(1)}%` : "観測不足"} ・ β({detail.price_risk.benchmark_symbol ?? "-"}) {detail.price_risk.beta_1y?.toFixed(2) ?? "観測不足"} ・ 下落時捕捉率 {detail.price_risk.downside_capture_1y?.toFixed(2) ?? "観測不足"}</p>
          <p className="detail-cagr">これは過去の実測値であり将来の予測ではありません。倍率帯はモデルの仮定、こちらは実際に起きたことです。</p>
        </div>
      )}

      {/* 30.2:取扱可否・流動性(フェーズ1) */}
      <div className="dd-section">
        <h3>取扱可否・流動性</h3>
        <p>
          <Term id="tradability">取扱可否</Term>:{" "}
          {detail.tradability === "tradable"
            ? `取扱あり(${detail.tradable_brokers.join("・")})`
            : detail.tradability === "not_listed"
              ? "リストにあるが対象外"
              : "未確認(取扱可否リストが未整備です)"}
        </p>
        <p>
          <Term id="adv">ADV(20営業日平均売買代金)</Term>:{" "}
          {detail.adv_usd != null ? formatMoney(detail.adv_usd) : "—"}
          {detail.adv_observation_days != null && detail.adv_observation_days < 20 && (
            <span className="th-badge">観測{detail.adv_observation_days}日(参考値)</span>
          )}
        </p>
        <p>
          <Term id="max_position">投入上限</Term>:{" "}
          {detail.max_position_usd != null ? formatMoney(detail.max_position_usd) : "—"}
          {detail.position_binding_constraint && (
            <span className="th-badge">
              {detail.position_binding_constraint === "liquidity" ? "板が制約" : "規律が制約"}
            </span>
          )}
        </p>
        <p className="detail-cagr">建てるのに {detail.days_to_build?.toFixed(1) ?? "—"} 日 ・ ストレス時に降りるのに {detail.days_to_exit_stressed?.toFixed(1) ?? "—"} 日</p>
        <p className="detail-cagr">ADV 平均 {detail.adv_usd != null ? formatMoney(detail.adv_usd) : "—"} / 中央値 {detail.adv_median_20d != null ? formatMoney(detail.adv_median_20d) : "—"} ・ 60日ゼロ出来高 {detail.zero_volume_days_60d}日</p>
      </div>

      {/* 30.4:提出書類とレッドフラグ */}
      <div className="dd-section">
        <h3>
          <Term id="red-flags">提出書類とレッドフラグ</Term>
        </h3>
        {detail.filings_checked_on == null ? (
          <p className="detail-cagr">未確認(追跡対象外のためEDGARをまだ見ていません)。</p>
        ) : detail.red_flags.length === 0 ? (
          <p className="detail-cagr">確認済み(最終確認 {detail.filings_checked_on})。該当するレッドフラグはありません。</p>
        ) : (
          <ul className="warning-list">
            {detail.red_flags.map((flag, i) => (
              <li key={`${flag.code}-${i}`}>
                <span
                  className={`warning-tag ${flag.severity === "blocking" ? "red-flag-blocking" : ""}`}
                >
                  {flag.severity === "blocking" ? "BLOCKING" : flag.severity === "warning" ? "WARNING" : "INFO"}
                </span>{" "}
                <span>{flag.detail}</span>{" "}
                {flag.document_url && (
                  <a href={flag.document_url} target="_blank" rel="noreferrer">
                    原本を見る
                  </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <FilingsTimelineSection ticker={detail.ticker} />
      <div className="dd-section"><h3>顧客集中・ガイダンス・訴訟</h3>
        <p>{detail.customer_concentration == null ? "顧客集中: 未取得" : detail.customer_concentration.length === 0 ? "顧客集中: 取得済み・該当なし" : `顧客集中: ${detail.customer_concentration.map((x) => `${x.period_end} ${x.customer_label} ${(x.revenue_pct * 100).toFixed(1)}%`).join(" / ")}`}</p>
        <p>{detail.guidance == null ? "ガイダンス: 未取得" : detail.guidance.length === 0 ? "ガイダンス: 取得済み・該当なし" : `ガイダンス ${detail.guidance.length}件`}</p>
        <p>{detail.litigation == null ? "訴訟: 未取得" : detail.litigation.length === 0 ? "訴訟: 取得済み・該当なし" : `訴訟 ${detail.litigation.length}件`}</p>
      </div>

      {/* 30.6:将来の希薄化見通し */}
      {detail.dilution_outlook && (
        <div className="dd-section">
          <h3>将来の希薄化見通し</h3>
          <p>
            直近3年のシェルフ登録(S-3/S-3ASR): {detail.dilution_outlook.shelf_filings.length}件 ・ 公募増資(424B5):{" "}
            {detail.dilution_outlook.offerings_last_3y}件
          </p>
          {(detail.dilution_outlook.shelf_filings.length > 0 || detail.dilution_outlook.offering_filings.length > 0) && (
            <ul className="warning-list">
              {[...detail.dilution_outlook.shelf_filings, ...detail.dilution_outlook.offering_filings]
                .sort((a, b) => (a.filed_date < b.filed_date ? 1 : -1))
                .map((f) => (
                  <li key={f.accession_number}>
                    {f.form} ({f.filed_date}){" "}
                    {f.document_url && (
                      <a href={f.document_url} target="_blank" rel="noreferrer">
                        原本
                      </a>
                    )}
                  </li>
                ))}
            </ul>
          )}
          <p>
            予約済み希薄化比率(シェルフ残枠+ATM残枠 ÷ 時価総額):{" "}
            {detail.dilution_outlook.reserved_dilution_ratio != null
              ? `${(detail.dilution_outlook.reserved_dilution_ratio * 100).toFixed(1)}%`
              : "未入力(投資ノートに remaining_shelf_capacity_usd / atm_remaining_usd を記入してください)"}
          </p>
        </div>
      )}

      {/* 30.5:SEC原本突合 */}
      {detail.sec_reconciliation.length > 0 && (
        <div className="dd-section">
          <h3>
            <Term id="sec-reconciliation">SEC原本突合</Term>
          </h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>概念</th>
                <th>モデル値(yfinance)</th>
                <th>SEC値(XBRL)</th>
                <th>差</th>
                <th>判定</th>
              </tr>
            </thead>
            <tbody>
              {detail.sec_reconciliation.map((item) => (
                <tr key={item.concept}>
                  <td>{item.concept}</td>
                  <td>{item.model_value != null ? item.model_value.toLocaleString() : "—"}</td>
                  <td>{item.sec_value != null ? item.sec_value.toLocaleString() : "—"}</td>
                  <td>{item.relative_diff != null ? `${(item.relative_diff * 100).toFixed(1)}%` : "—"}</td>
                  <td>
                    {item.status === "match" && "一致"}
                    {item.status === "mismatch" && <span className="warning-tag">不一致</span>}
                    {item.status === "magnitude_mismatch" && (
                      <span className="warning-tag red-flag-blocking">桁違い</span>
                    )}
                    {item.status === "unavailable" && "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* J-3:バリュエーションの現在地。モデルの入力ではなく、人間が読むための断面情報。 */}
      {(detail.week52_position != null ||
        PERCENTILE_ROWS.some((r) => factorNumber(detail.factors, r.key) != null)) && (
        <div className="dd-section">
          <h3>バリュエーションの現在地</h3>
          <p className="detail-cagr">
            分位は「同じ日の断面」での相対位置です。<strong>順位計算には一切影響しません</strong>
            ——成長の対価の差し引き(κ)はモデル内部で既に済んでいます。ここは「高いのか安いのか」を
            人間が読むための情報です。
          </p>
          {factorNumber(detail.factors, "current_ev_to_gross_profit") != null && (
            <p>
              現在の EV/粗利:{" "}
              <strong>{factorNumber(detail.factors, "current_ev_to_gross_profit")!.toFixed(1)}x</strong>
            </p>
          )}
          <table className="data-table">
            <tbody>
              {PERCENTILE_ROWS.map((row) => {
                const pct = factorNumber(detail.factors, row.key);
                return (
                  <tr key={row.key}>
                    <td>{row.label}</td>
                    <td>
                      {pct == null ? (
                        <span className="detail-cagr">セクター標本が少ないため非表示</span>
                      ) : (
                        <>
                          <div className="factor-bar">
                            <div
                              className="factor-bar-fill positive"
                              style={{ width: `${(pct * 100).toFixed(0)}%` }}
                            />
                          </div>
                          第{(pct * 100).toFixed(0)}分位
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {detail.week52_high != null && detail.week52_low != null && (
            <p>
              52週レンジ: ${detail.week52_low.toFixed(2)} 〜 ${detail.week52_high.toFixed(2)}
              {detail.week52_position != null ? (
                <>
                  {" "}
                  ・ 現在値はレンジの <strong>{(detail.week52_position * 100).toFixed(0)}%</strong> の位置
                  <div className="factor-bar">
                    <div
                      className="factor-bar-fill positive"
                      style={{ width: `${(detail.week52_position * 100).toFixed(0)}%` }}
                    />
                  </div>
                </>
              ) : (
                " ・ 直近1年で値動きがありません"
              )}
            </p>
          )}
          {detail.score_history.some((p) => p.ev_to_gross_profit != null) && (
            <>
              <h4>自社の EV/粗利の推移(直近)</h4>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart
                  data={[...detail.score_history]
                    .reverse()
                    .map((p) => ({ score_date: p.score_date, ev: p.ev_to_gross_profit ?? null }))}
                >
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="score_date" />
                  <YAxis domain={["auto", "auto"]} tickFormatter={(v) => `${Number(v).toFixed(1)}x`} width={55} />
                  <Tooltip formatter={(v) => [`${Number(v).toFixed(2)}x`, "EV/粗利"]} />
                  <Line type="monotone" dataKey="ev" stroke="#2563eb" dot={false} connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </>
          )}
        </div>
      )}

      {detail.factor_breakdown.length > 0 && (
        <div className="factor-breakdown">
          <h3>スコアの内訳</h3>
          <p className="factor-intro">
            期待倍率は下の5因子の<strong>積</strong>です(15.1の恒等式)。各因子の値は
            「その因子が単独で実現倍率を何倍にしているか」で、1.00が中立です。
            {detail.target &&
              !detail.target.is_default &&
              `この内訳は「${detail.target.horizon_years}年で${detail.target.target_moic}倍」で計算し直した値です。`}
          </p>
          <div className="factor-list">
            {detail.factor_breakdown.map((f) => {
              const isPositive = f.contribution >= 1;
              return (
                <div key={f.key} className="factor-card">
                  <div className="factor-card-header">
                    <h4>{f.label}</h4>
                    <span className={`factor-contribution ${isPositive ? "positive" : "negative"}`}>
                      ×{f.contribution.toFixed(2)}
                    </span>
                  </div>
                  <div className="factor-bar">
                    {/* 中立(1.0)を中心に、対数スケールで左右へ伸ばす */}
                    <div
                      className={`factor-bar-fill ${isPositive ? "positive" : "negative"}`}
                      style={{
                        width: `${Math.min(Math.abs(Math.log(Math.max(f.contribution, 1e-3))) * 40, 50)}%`,
                        marginLeft: isPositive ? "50%" : undefined,
                        marginRight: isPositive ? undefined : "50%",
                        transform: isPositive ? undefined : "translateX(0)",
                      }}
                    />
                  </div>
                  <p className="factor-explanation">{f.explanation}</p>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {detail.factors && (
        <div className="diagnostics">
          <h3>診断値</h3>
          <table className="data-table">
            <tbody>
              {DIAGNOSTICS.filter((d) => typeof detail.factors?.[d.key] === "number").map((d) => (
                <tr key={d.key}>
                  <td>{d.term ? <Term id={d.term}>{d.label}</Term> : d.label}</td>
                  <td>{d.format(detail.factors![d.key] as number)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail.score_history.length > 1 && (
        <div className="score-history">
          <h3>P(10倍)の推移(既定の7年/10倍)</h3>
          <ScoreHistoryChart data={detail.score_history} />
        </div>
      )}

      {/* J-2:実績の推移(売上・粗利率・CF・現金・株式数・ランウェイ・F-score内訳) */}
      <FinancialHistorySection ticker={detail.ticker} />
      <PeerComparisonSection ticker={detail.ticker} />

      {/* J-7:需給(インサイダー・空売り残・浮動株)。バッジは出すが警告色にしない。 */}
      {detail.supply && (
        <div className="dd-section">
          <h3>需給</h3>
          <p className="detail-cagr">
            <strong>順位計算には一切入っていません</strong>(原則3)。インサイダー売却は権利行使・納税・
            分散のいずれでも起きるため、ここでは色で断定しません。
          </p>
          <p>
            インサイダー(直近180日):{" "}
            {detail.supply.insider_net_shares_180d != null
              ? `ネット ${detail.supply.insider_net_shares_180d >= 0 ? "+" : ""}${detail.supply.insider_net_shares_180d.toLocaleString()} 株 ・ 買い手 ${detail.supply.insider_buyer_count_180d ?? 0} 名`
              : "未取得"}
            {detail.supply.insider_as_of && ` (最終取引 ${detail.supply.insider_as_of})`}
          </p>
          <p>
            空売り残:{" "}
            {detail.supply.short_interest_shares != null
              ? `${detail.supply.short_interest_shares.toLocaleString()} 株`
              : "未取得"}
            {detail.supply.days_to_cover != null && ` ・ 日数カバー ${detail.supply.days_to_cover.toFixed(1)}日`}
            {detail.supply.short_as_of && (
              <span className="th-badge">
                {detail.supply.short_as_of} 時点
                {detail.supply.short_lag_days != null && `・${detail.supply.short_lag_days}日遅れ`}
              </span>
            )}
          </p>
          <p>
            浮動株:{" "}
            {detail.supply.public_float_usd != null
              ? `$${(detail.supply.public_float_usd / 1e6).toFixed(0)}M`
              : "未取得"}
            {detail.supply.float_ratio != null && ` ・ 時価総額比 ${(detail.supply.float_ratio * 100).toFixed(0)}%`}
          </p>
        </div>
      )}

      {/* J-5:デューデリ・チェックリスト(11工程)+ 一次情報への導線 */}
      <DueDiligenceChecklist detail={detail} />

      {/* K-9:生成AIによる定性分析(参考)。**最下部に置く**——上に置くと
          定量モデルの出力より先に読まれ、順位の根拠だと受け取られる。 */}
      <LlmAnalysisSection ticker={detail.ticker} />

      {/* Phase 8(Issue #3 §28・§29):v5 Shadow Challengerは既存v4画面の
          さらに下に追加専用で置く——v4の表示・挙動は一切変えない。 */}
      <V5TickerDetailSection
        ticker={detail.ticker}
        v4Probability={detail.probability}
        v4ExpectedMoic={detail.expected_moic}
        objective={v5Objective}
        asOf={v5AsOf}
      />
    </div>
  );
}
