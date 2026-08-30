import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchLatestBacktest } from "../api/client";
import { Term } from "../components/Term";
import type { GlossaryId } from "../glossary";
import type { BacktestSummary } from "../api/types";

/**
 * モデルの検証状況を常時見せるページ(27.8・14.2)。
 *
 * ランキングだけを見せてこの情報を隠すのは、ツールとして誤った確信を与える。
 * 14.2は「上位デシルでも大半は外れる前提をUI上にも明示すること」を要件と
 * しており、KPIが目標に届いていない事実もそのまま出す。
 */

function pct(v: number | null | undefined, digits = 1): string {
  return v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
}

interface KpiRow {
  label: string;
  /** 用語集(28.18)のid。付いていればラベルにツールチップが出る */
  term?: GlossaryId;
  value: string;
  target: string;
  passed: boolean | null;
  note: string;
}

/**
 * 28.11:14.2の「リフト倍率 >= 2.0」は、本来「10バガー達成率」という極端に稀な
 * 事象に対する指標だった。27.12がそれを「1年で1.389倍」に読み替えたが、
 * その事象の基準率は約25%であり右裾ではない。目標値だけを持ち越したのは
 * 読み替えの誤りなので、KPI表の主指標は「断面上位5%の事象」に対するリフトにする。
 */
const HEADLINE_TAIL_QUANTILE = 0.05;

function buildKpis(data: BacktestSummary): KpiRow[] {
  const tail = data.tail_lifts.find((t) => Math.abs(t.quantile - HEADLINE_TAIL_QUANTILE) < 1e-9);
  const lift = data.lift_ratio;
  const mono = data.decile_monotonicity;
  const lossOk =
    data.top_decile_loss_rate != null && data.universe_loss_rate != null
      ? data.top_decile_loss_rate < data.universe_loss_rate
      : null;
  return [
    {
      label: "デシル単調性",
      term: "monotonicity",
      value: mono != null ? mono.toFixed(3) : "—",
      target: "+1.0に近いこと",
      passed: mono != null ? mono > 0.7 : null,
      note: "スコア上位から下位へ、将来リターンの中央値が単調に下がっているか(順位相関)。14.2は絶対値よりこれを重視する。",
    },
    {
      label: "右裾リフト(上位5%の事象)",
      term: "lift",
      value: tail != null ? `${tail.lift.toFixed(2)}x` : "—",
      target: "2.0以上",
      passed: tail != null ? tail.lift >= 2.0 : null,
      note:
        "その評価日に断面リターン上位5%へ入った銘柄を、モデル上位10%がどれだけ多く捕まえたか。" +
        "10バガー探索としての性能に最も近い指標(28.11)。閾値を断面分位で決めているので、" +
        "強気相場でも弱気相場でも基準率は5%に固定される。",
    },
    {
      label: "オンペース・リフト(参考)",
      term: "on-pace",
      value: lift != null ? `${lift.toFixed(2)}x` : "—",
      target: "—(基準率が高く2.0は非現実的)",
      passed: null,
      note:
        "上位デシルのオンペース率 ÷ ユニバース全体のオンペース率。ただしこの「オンペース」" +
        "(10倍/7年と同じ年率)の基準率は約25%あり、右裾の事象ではない。" +
        "ここで2.0を出すには上位10%の50%が達成する必要があり、要求として過大である(28.11)。",
    },
    {
      label: "破綻回避率",
      term: "loss-rate",
      value: `${pct(data.top_decile_loss_rate)} vs ${pct(data.universe_loss_rate)}`,
      target: "上位デシルのほうが低いこと",
      passed: lossOk,
      note: "−50%以下まで下落した銘柄の割合(上位デシル vs ユニバース全体)。",
    },
    {
      label: "較正誤差",
      term: "calibration",
      value: data.calibration_error != null ? `${(data.calibration_error * 100).toFixed(2)}pt` : "—",
      target: "0に近いこと",
      passed: data.calibration_error != null ? Math.abs(data.calibration_error) < 0.05 : null,
      note: "モデルが出す確率をホライズンに引き直した予測値と、実際の達成率の差。正なら強気すぎ、負なら弱気すぎ。",
    },
    {
      label: "順位IC",
      term: "rank-ic",
      value: data.rank_ic != null ? data.rank_ic.toFixed(3) : "—",
      target: "0より十分大きいこと",
      passed: data.rank_ic != null ? data.rank_ic > 0.03 : null,
      note:
        "評価日ごとに「確率の順位」と「実現リターンの順位」の相関を取り、その平均。" +
        "上位デシルだけでなく断面全体で序列が合っているかを見る。" +
        (data.rank_ic_t_stat != null
          ? ` t値 ${data.rank_ic_t_stat.toFixed(1)}(評価日を独立とみなした上限値)。`
          : ""),
    },
    {
      label: "最悪の評価日のリフト",
      value: data.lift_ratio_worst_date != null ? `${data.lift_ratio_worst_date.toFixed(2)}x` : "—",
      target: "1.0以上",
      passed: data.lift_ratio_worst_date != null ? data.lift_ratio_worst_date >= 1.0 : null,
      note:
        "平均が良くても、効かない時期があれば実運用では耐えられない。" +
        "評価日ごとの内訳は下の表を参照(28.9)。",
    },
    {
      label: "ナウキャスト上限への張り付き率",
      value: data.nowcast_cap_hit_rate != null ? pct(data.nowcast_cap_hit_rate) : "—",
      target: "監視のみ(目標値なし)",
      passed: null,
      note:
        "S-8(2026-08-26の監査で追加)。価格トレンドによる成長率補正が上限に張り付いている" +
        "観測の割合。高いほど「決算にもとづく補正」のはずが実質的にモメンタムをそのまま" +
        "反映している状態に近づく。狭い上限への変更は実測でKPIを悪化させたため未採用(詳細は" +
        "model_audit_v4_2026-08-26.md S-8)。",
    },
  ];
}

export function ValidationPage() {
  const [data, setData] = useState<BacktestSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchLatestBacktest()
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>読み込み中...</p>;
  if (error) return <p className="error">エラー: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <h2>モデルの検証状況</h2>

      <div className="model-notice">
        <strong>このモデルはまだ検証途中です。</strong> 下の数値は擬似バックテスト
        ——過去の各時点で「その時に開示済みだったデータだけ」からスコアを付け直し、
        以降の実現リターンと突き合わせた結果——です。ランキングを見る前に、
        モデルがどの程度当たっているのか(いないのか)をここで確認してください。
        <br />
        このページには専門用語が多く出ます。点線の下線が付いた言葉はマウスを乗せると
        説明が出ます(一覧は<Link to="/glossary">用語集</Link>)。
      </div>

      {/* S-9(model_audit_v4_2026-08-26.md):σの縮小推定により、順位は実質的に
          リスク未調整の期待倍率の順序に近い、という実測結果の明示 */}
      <div className="model-notice">
        <strong>順位はリスクをほとんど反映していません。</strong> σ(ばらつき)の推定を
        断面中心へ85%縮小しているため(28.4)、実測では<Term id="survival">生存確率</Term>と
        順位の相関はほぼゼロです。「P(目標倍率)」という数字にもかかわらず、
        <strong>実質的な序列は生存確率で調整する前の期待倍率の順序に近い</strong>状態です。
        生存確率が低い銘柄はランキング画面・銘柄詳細で警告バッジが付きますが、
        除外はされません。
      </div>

      {data.observation_count === 0 ? (
        <p>
          バックテストがまだ実行されていません。
          <code>uv run python -m autoscreener.cli run-backtest</code> を実行してください。
        </p>
      ) : (
        <>
          <p className="score-date">
            実行: {data.run_at ? new Date(data.run_at).toLocaleString("ja-JP") : "—"} ・ モデル{" "}
            {data.scoring_version} ・ 観測数 {data.observation_count.toLocaleString()} ・ ホライズン{" "}
            {data.horizon_years?.toFixed(2)}年
          </p>

          <h3>14.2 のKPI</h3>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>指標</th>
                  <th>実測</th>
                  <th>目標</th>
                  <th>判定</th>
                  <th>意味</th>
                </tr>
              </thead>
              <tbody>
                {buildKpis(data).map((k) => (
                  <tr key={k.label}>
                    <td>{k.term ? <Term id={k.term}>{k.label}</Term> : k.label}</td>
                    <td>
                      <strong>{k.value}</strong>
                    </td>
                    <td>{k.target}</td>
                    <td>
                      <span className={`kpi-badge ${k.passed === null ? "unknown" : k.passed ? "pass" : "fail"}`}>
                        {k.passed === null ? "—" : k.passed ? "達成" : "未達"}
                      </span>
                    </td>
                    <td className="kpi-note">{k.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {!data.is_calibrated && (
            <div className="model-notice">
              <strong>この実行では確率の較正写像が学習されていません。</strong>{" "}
              表示される確率はモデルの対数正規仮定そのままの値であり、実測頻度で
              裏打ちされていません(28.8)。
            </div>
          )}

          <h3>右裾リフト —— 事象をどこまで右へずらせるか</h3>
          <p className="factor-intro">
            <strong>
              <Term id="tenbagger">10バガー</Term>探索ツールの性能は、平均的な勝ちではなく
              右裾で測るべきです。
            </strong>{" "}
            下の表は「その評価日に断面リターン上位◯%へ入った銘柄」を当たりと定義したときの、
            モデル上位10%のリフトです。閾値を断面の分位で決めているため基準率はどの評価日でも
            一定になり、相場つきの影響を受けません(28.11)。
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>当たりの定義</th>
                  <th>閾値リターン(中央値)</th>
                  <th>モデル上位10%の的中率</th>
                  <th>リフト</th>
                  <th>最悪の評価日</th>
                </tr>
              </thead>
              <tbody>
                {data.tail_lifts.map((t) => (
                  <tr key={t.quantile}>
                    <td>断面リターン上位 {(t.quantile * 100).toFixed(0)}%</td>
                    <td>
                      {t.median_threshold_return >= 0 ? "+" : ""}
                      {pct(t.median_threshold_return)}
                    </td>
                    <td>{pct(t.top_decile_hit_rate)}</td>
                    <td>
                      <strong>{t.lift.toFixed(2)}x</strong>
                    </td>
                    <td className={t.worst_date_lift >= 1 ? "positive" : "negative"}>
                      {t.worst_date_lift.toFixed(2)}x
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3><Term id="decile">デシル</Term>別の実績</h3>
          <p className="factor-intro">
            デシル1 = モデルが最も有望と判定した10%。
            <Term id="on-pace">オンペース率</Term>は「10倍/7年と同じ年率(38.9%)を、
            この保有期間で達成した割合」です。
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>デシル</th>
                  <th>銘柄数</th>
                  <th>平均予測P(10倍)</th>
                  <th>中央値リターン</th>
                  <th>オンペース率</th>
                  <th>
                    <Term id="loss-rate">−50%以下の割合</Term>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.deciles.map((d) => (
                  <tr key={d.decile}>
                    <td className="rank-cell">{d.decile}</td>
                    <td>{d.count}</td>
                    <td>{pct(d.mean_probability, 3)}</td>
                    <td className={d.median_return >= 0 ? "positive" : "negative"}>
                      {d.median_return >= 0 ? "+" : ""}
                      {pct(d.median_return)}
                    </td>
                    <td>{pct(d.on_pace_rate)}</td>
                    <td>{pct(d.loss_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {data.per_date.length > 0 && (
            <>
              <h3>評価日ごとの内訳</h3>
              <p className="factor-intro">
                <strong>平均だけを見ると検出力の低さが隠れます。</strong>{" "}
                保有期間が重なっているため独立な観測期間は評価日数よりはるかに少なく、
                いずれかの評価日でリフトが1を割っていれば「常に効く」とは言えません(28.9)。
              </p>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>評価日</th>
                      <th>銘柄数</th>
                      <th>ユニバースのオンペース率</th>
                      <th>上位デシル</th>
                      <th>リフト</th>
                      <th>
                        <Term id="rank-ic">順位IC</Term>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.per_date.map((d) => (
                      <tr key={d.base_date}>
                        <td>{d.base_date}</td>
                        <td>{d.count}</td>
                        <td>{pct(d.universe_on_pace_rate)}</td>
                        <td>{pct(d.top_decile_on_pace_rate)}</td>
                        <td className={d.lift_ratio >= 1 ? "positive" : "negative"}>
                          {d.lift_ratio.toFixed(2)}x
                        </td>
                        <td className={d.rank_ic >= 0 ? "positive" : "negative"}>{d.rank_ic.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {data.calibration_curve.length > 0 && (
            <>
              <h3>較正曲線 —— 確率の水準そのものは合っているか</h3>
              <p className="factor-intro">
                較正誤差は平均どうしの差しか見ないため、「全体としては合っているが、高い確率を
                出した銘柄では外している」という形の誤りを見逃します。予測確率の階級ごとに
                実測頻度と並べると、<strong>どの確率帯で外しているか</strong>が分かります(28.8)。
                実測が予測より低ければモデルは強気すぎ、高ければ弱気すぎです。
              </p>
              <p className="factor-intro">
                <strong>較正は観測した範囲の外へは外挿しません。</strong>{" "}
                最上位の帯より高い確率を出した銘柄には、その帯の実測頻度がそのまま当てられます。
                結果として上位数十銘柄の「1年オンペース率」はほぼ同じ値に張り付きますが、
                これは<em>そこから先を区別できるだけの観測が無い</em>という正直な表明です
                (無理に外挿すれば、裏付けの無い数字を出すことになります)。
                順位そのものは P(10倍) 側で保たれています。
              </p>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>予測確率の帯</th>
                      <th>銘柄数</th>
                      <th>平均予測</th>
                      <th>実測頻度</th>
                      <th>ずれ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.calibration_curve.map((b) => (
                      <tr key={`${b.lower}-${b.upper}`}>
                        <td>
                          {pct(b.lower, 2)} 〜 {pct(b.upper, 2)}
                        </td>
                        <td>{b.count}</td>
                        <td>{pct(b.mean_predicted, 2)}</td>
                        <td>{pct(b.realized_rate, 2)}</td>
                        <td className={b.mean_predicted - b.realized_rate <= 0 ? "positive" : "negative"}>
                          {((b.mean_predicted - b.realized_rate) * 100).toFixed(2)}pt
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {data.asset_correlation != null && (
            <>
              <h3>銘柄間の相関</h3>
              <p className="factor-intro">
                評価日ごとの的中率の散らばりから推定した
                <Term id="asset-correlation">資産相関</Term>は{" "}
                <strong>{data.asset_correlation.toFixed(3)}</strong>{" "}
                です(観測数によるノイズ分は差し引き済み)。これが0より大きいということは、
                <strong>10倍銘柄の発生は銘柄ごとに独立ではない</strong>
                ——当たる時期には多くの銘柄が当たり、外れる時期にはどれも外れる——ことを意味します。
                ランキング画面の「ポートフォリオとしての見通し」はこの値を使って
                「少なくとも1つ当たる確率」を計算しています(28.12)。
              </p>
            </>
          )}

          <h3>この結果を読むときの留保事項</h3>
          <ul className="caveat-list">
            {data.caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
