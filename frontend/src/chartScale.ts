/**
 * グラフの縦軸を実データのレンジに合わせるためのヘルパー。
 *
 * recharts の `YAxis` は既定で `[0, 'auto']`——常に 0 を含む——ため、
 * 4% 付近を上下する米10年債利回りのように「0.数pt の差」が意味を持つ系列だと、
 * その差が軸のスケールに埋もれて読み取れなくなる。ここでは 0 基準に固定せず、
 * 実データの最小〜最大に少しだけ余白を足した範囲を返す。
 */

type Num = number | null | undefined;

function finite(values: Num[]): number[] {
  return values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
}

/**
 * 実データのレンジ + 余白(既定 8%)を `[min, max]` で返す。
 * データが無ければ `undefined`(呼び出し側で既定ドメインにフォールバックする)。
 * `clampMin` を渡すと下限をそこで止める(確率や比率で軸が負に食い込むのを防ぐ)。
 */
export function fittedDomain(
  values: Num[],
  opts: { padFraction?: number; clampMin?: number } = {},
): [number, number] | undefined {
  const { padFraction = 0.08, clampMin } = opts;
  const nums = finite(values);
  if (nums.length === 0) return undefined;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  if (min === max) {
    const p = Math.abs(min) * 0.01 || 1;
    return [clampMin != null ? Math.max(clampMin, min - p) : min - p, max + p];
  }
  const pad = (max - min) * padFraction;
  const lo = min - pad;
  return [clampMin != null ? Math.max(clampMin, lo) : lo, max + pad];
}

/**
 * レンジの広さから軸ラベルの小数桁を決める。
 * 金利・スプレッド(レンジ数pt)は 2 桁、為替(レンジ数十)は 1 桁、
 * 指数など(レンジ数百)は 0 桁。
 */
export function tickDecimals(values: Num[]): number {
  const nums = finite(values);
  if (nums.length === 0) return 2;
  const span = Math.max(...nums) - Math.min(...nums);
  if (span < 5) return 2;
  if (span < 50) return 1;
  return 0;
}
