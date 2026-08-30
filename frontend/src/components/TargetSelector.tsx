import type { TargetSpec } from "../api/types";

/**
 * 「何年で何倍」を選ぶコントロール(27.24)。
 *
 * **必要年率を常に併記するのが設計の要点。** 年数と倍率を別々に眺めていると
 * 難易度を取り違える——「3年で3倍」(年率44.2%)は「7年で10倍」(38.9%)より
 * 厳しい、という関係は年率に直さないと見えない。プリセットにも年率を出す。
 */

export interface TargetChoice {
  horizonYears: number;
  targetMoic: number;
}

const PRESETS: { label: string; value: TargetChoice }[] = [
  { label: "3年で3倍", value: { horizonYears: 3, targetMoic: 3 } },
  { label: "5年で5倍", value: { horizonYears: 5, targetMoic: 5 } },
  { label: "7年で10倍", value: { horizonYears: 7, targetMoic: 10 } },
  { label: "10年で10倍", value: { horizonYears: 10, targetMoic: 10 } },
];

function cagr(choice: TargetChoice): number {
  return choice.targetMoic ** (1 / choice.horizonYears) - 1;
}

/** ドル建ての規模を「$3.5B」のように読みやすく書く。 */
function usd(value: number): string {
  if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(0)}M`;
  return `$${value.toFixed(0)}`;
}

/** 必要年率から見た難易度。歴史的にどのくらい珍しいかの目安。 */
function difficulty(rate: number): { label: string; tone: string } {
  if (rate >= 0.5) return { label: "極めて厳しい", tone: "hard" };
  if (rate >= 0.35) return { label: "厳しい", tone: "hard" };
  if (rate >= 0.22) return { label: "野心的", tone: "mid" };
  if (rate >= 0.12) return { label: "現実的", tone: "easy" };
  return { label: "控えめ", tone: "easy" };
}

export function TargetSelector({
  value,
  onChange,
  effective,
}: {
  value: TargetChoice;
  onChange: (next: TargetChoice) => void;
  effective: TargetSpec | null;
}) {
  const rate = effective ? effective.required_cagr : cagr(value);
  const level = difficulty(rate);

  return (
    <section className="target-selector">
      <div className="target-header">
        <h3>目標を設定する</h3>
        <div className={`target-cagr tone-${level.tone}`}>
          必要年率 <strong>{(rate * 100).toFixed(1)}%</strong>
          <span className="target-difficulty">{level.label}</span>
        </div>
      </div>

      <div className="target-presets">
        {PRESETS.map((p) => {
          const active = p.value.horizonYears === value.horizonYears && p.value.targetMoic === value.targetMoic;
          return (
            <button
              key={p.label}
              type="button"
              className={`target-preset${active ? " active" : ""}`}
              onClick={() => onChange(p.value)}
            >
              <span className="preset-label">{p.label}</span>
              <span className="preset-cagr">年率 {(cagr(p.value) * 100).toFixed(1)}%</span>
            </button>
          );
        })}
      </div>

      <div className="target-custom">
        <label>
          年数
          <input
            type="number"
            min={1}
            max={15}
            step={1}
            value={value.horizonYears}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1 && n <= 15) onChange({ ...value, horizonYears: n });
            }}
          />
          <span className="unit">年</span>
        </label>
        <label>
          倍率
          <input
            type="number"
            min={1.5}
            max={100}
            step={0.5}
            value={value.targetMoic}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1.5 && n <= 100) onChange({ ...value, targetMoic: n });
            }}
          />
          <span className="unit">倍</span>
        </label>
      </div>

      {effective && (
        <p className="target-universe">
          この目標のユニバース:<strong>時価総額 {usd(effective.market_cap_ceiling)}未満</strong>
          ・売上高 {usd(effective.revenue_ceiling)}未満
          {effective.universe_ceiling_capped ? (
            <>
              {" "}
              <span className="target-universe-capped">
                (母集団はここで頭打ちです。日次バッチが集計している範囲は「3倍」相当までのため、
                これより緩い目標にしてもユニバースはこれ以上広がりません)
              </span>
            </>
          ) : (
            <>
              {" "}
              <span className="target-universe-note">
                上限は目標倍率に追随します(出口 $35B ÷ 目標倍率)。「大きすぎる企業は算数上
                10倍になれない」という除外の根拠は、10倍という目標に依存しているためです。
              </span>
            </>
          )}
        </p>
      )}

      <p className="target-note">
        目標を変えると、保存済みの入力から<strong>その年数で計算し直して</strong>並べ替えます
        (成長の減衰・希薄化の複利・生存確率をすべて再計算するので、時間方向に引き伸ばした
        近似ではありません)。年数を短くすると、複利が効かず期待倍率が1.0を割る銘柄が増えるため
        <strong>対象銘柄数も減ります</strong>。一方で、目標を緩めると上限が緩んで
        <strong>より大きな企業が候補に入ります</strong>——両方が同時に効くので、
        銘柄数は単純には増減しません。
      </p>
    </section>
  );
}
