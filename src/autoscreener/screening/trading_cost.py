"""取引コストの推定(docs/defect_and_edge_audit_2026-08-28.md D-5 / I-7)。すべて純粋関数。

**新しいデータ取得は不要。** 既存の `price_snapshots`(14.11で正規化済みのOHLCV)
だけで、実効スプレッドとマーケットインパクトを推定する。

- `corwin_schultz_spread` … 2日分の高値・安値から相対スプレッドを出す標準的な
  推定量(Corwin & Schultz 2012)。板気配データが無くても実効スプレッドが測れる。
- `amihud_illiquidity` … |日次リターン| ÷ 売買代金 の平均。価格が売買代金に対して
  どれだけ動きやすいか=マーケットインパクトの代理。
- `round_trip_cost_bps` … 上2つと建玉サイズから、往復の取引コストを bps で返す。

D-5 の指摘:`runner._realized_return` は `exit/entry - 1` をそのまま返しており、
手数料もスプレッドもマーケットインパクトも無い。ユニバース下限の
`min_daily_dollar_volume_usd: 1_000_000` 帯のマイクロキャップは往復スプレッドが
0.5〜3% になる。このモジュールでその欠落を、仮定ではなく実測で埋める。
"""

from __future__ import annotations

import math

# Corwin–Schultz の定数 3 - 2*sqrt(2)。
_K = 3 - 2 * math.sqrt(2)


def corwin_schultz_spread(
    bars: list[tuple[float, float]], window: int = 20
) -> float | None:
    """(high, low) の時系列から相対実効スプレッドを推定する(Corwin & Schultz 2012)。

    `bars` は時系列順(古い→新しい)。末尾 `window + 1` 本を使い、隣り合う2日
    ごとに推定量を出して平均する。負の推定値は 0 に丸める(原論文の推奨。
    高ボラティリティ日に測定式が負へ振れることがある)。

    有効な隣接ペアが1つも作れなければ None。返り値は相対値(0.01 = 1%)。
    """
    clean = [
        (h, l)
        for h, l in bars
        if h is not None and l is not None and h > 0 and l > 0 and h >= l
    ]
    if len(clean) < 2:
        return None
    clean = clean[-(window + 1) :]

    estimates: list[float] = []
    for (h1, l1), (h2, l2) in zip(clean, clean[1:]):
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        high2 = max(h1, h2)
        low2 = min(l1, l2)
        gamma = math.log(high2 / low2) ** 2
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
        spread = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
        estimates.append(max(spread, 0.0))

    if not estimates:
        return None
    return sum(estimates) / len(estimates)


def amihud_illiquidity(
    returns: list[float], dollar_volumes: list[float]
) -> float | None:
    """Amihud 非流動性 = mean(|return| / dollar_volume)。

    大きいほど「同じ売買代金でも価格が動きやすい」=インパクトが大きい。
    `returns` と `dollar_volumes` は同じ日付で対応していること。売買代金が
    0/欠損の日は捨てる。
    """
    pairs = [
        (abs(r), dv)
        for r, dv in zip(returns, dollar_volumes)
        if r is not None and dv is not None and dv > 0
    ]
    if not pairs:
        return None
    return sum(ar / dv for ar, dv in pairs) / len(pairs)


def round_trip_cost_bps(
    spread: float | None,
    position_usd: float,
    adv_usd: float | None,
    impact_coefficient: float,
    *,
    commission_bps: float = 0.0,
    min_half_spread_bps: float = 0.0,
) -> float:
    """往復(建て+決済)の取引コストを bps で返す。

    - スプレッド費用:片道で半スプレッド、往復で1スプレッド ≈ `spread` を bps 化。
    - マーケットインパクト:平方根則 `impact = k * sqrt(position / ADV)` を片道に
      課し、往復で2倍。ADV が無ければインパクトは0(スプレッドと手数料のみ)。
    - `commission_bps` は往復合計の手数料(定額の口座なら0でよい)。

    `spread` が None のときは `2 * min_half_spread_bps` を下限として使う。
    """
    half_spread_bps = (spread * 10_000 / 2) if spread is not None else min_half_spread_bps
    half_spread_bps = max(half_spread_bps, min_half_spread_bps)
    spread_cost_bps = 2 * half_spread_bps

    impact_bps = 0.0
    if adv_usd is not None and adv_usd > 0 and position_usd > 0:
        impact_one_way = impact_coefficient * math.sqrt(position_usd / adv_usd)
        impact_bps = 2 * impact_one_way * 10_000

    return spread_cost_bps + impact_bps + commission_bps
