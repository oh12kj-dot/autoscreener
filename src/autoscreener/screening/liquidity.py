"""流動性とポジションサイズ上限(30.2.2)。

元文書 第06節・第11節。ポジションサイズの上限を決めるのはモデルの確信度では
なく板の厚さである、という原則を数字にする。**新しいデータ取得は不要**で、
既存の `price_snapshots`(14.11で正規化済みのOHLCV)だけで計算できる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

ADV_WINDOW_DAYS = 20  # 元文書 第06節「20日平均売買代金」
MIN_OBSERVATION_DAYS = 5  # これ未満は平均と呼べないので None を返す

LIQUIDITY_BINDING = "liquidity"
PORTFOLIO_BINDING = "portfolio"


@dataclass(frozen=True)
class LiquidityProfile:
    adv_usd: float | None  # 20営業日平均売買代金(終値 × 出来高)
    observation_days: int  # 実際に使えた営業日数。20未満なら参考値
    max_position_adv_usd: float | None  # ADV × adv_participation_cap
    max_position_portfolio_usd: float | None  # 総資産 × per_position_cap
    max_position_usd: float | None  # 上2つの小さいほう(元文書 第11節)
    binding_constraint: str | None  # "liquidity" / "portfolio" / None
    # L-6 display-only execution diagnostics.  Existing sizing fields above
    # intentionally retain their original formulas.
    adv_median_20d: float | None = None
    adv_stress: float | None = None
    zero_volume_days_60d: int = 0
    days_to_build: float | None = None
    days_to_exit_stressed: float | None = None


def compute_liquidity_profile(
    closes_and_volumes: list[tuple[float | None, int | None]],
    *,
    portfolio_value_usd: float | None,
    adv_participation_cap: float,
    per_position_cap: float,
) -> LiquidityProfile:
    """純粋関数。DBには触らない。呼び出し元が直近20営業日ぶんを渡す。

    `closes_and_volumes` は新しい順である必要はない——単純平均なので順序に
    依存しない。ここでは先頭から `ADV_WINDOW_DAYS` 件までを使う契約とし、
    それ以上は呼び出し元が渡さない(クエリ側で20件に絞る)ことを前提にする。
    """
    usable = [
        (close, volume)
        for close, volume in closes_and_volumes[:ADV_WINDOW_DAYS]
        if close is not None and volume is not None
    ]
    observation_days = len(usable)

    if observation_days < MIN_OBSERVATION_DAYS:
        adv_usd = None
    else:
        dollar_volumes = [close * volume for close, volume in usable]
        adv_usd = sum(dollar_volumes) / len(dollar_volumes)

    if adv_usd is None:
        max_position_adv_usd = None
    else:
        max_position_adv_usd = adv_usd * adv_participation_cap

    if portfolio_value_usd is None:
        max_position_portfolio_usd = None
    else:
        max_position_portfolio_usd = portfolio_value_usd * per_position_cap

    candidates = [v for v in (max_position_adv_usd, max_position_portfolio_usd) if v is not None]
    if not candidates:
        max_position_usd = None
        binding_constraint = None
    else:
        max_position_usd = min(candidates)
        # どちらの制約が効いているかを返すのが要点(30.2.2)。両方が候補にあり
        # 値が等しい場合は流動性側を優先して報告する(板が薄いことに気づける
        # ほうが、規律側だけを見せるより実務上重要なため)。
        if max_position_adv_usd is not None and max_position_usd == max_position_adv_usd:
            binding_constraint = LIQUIDITY_BINDING
        else:
            binding_constraint = PORTFOLIO_BINDING

    return LiquidityProfile(
        adv_usd=adv_usd,
        observation_days=observation_days,
        max_position_adv_usd=max_position_adv_usd,
        max_position_portfolio_usd=max_position_portfolio_usd,
        max_position_usd=max_position_usd,
        binding_constraint=binding_constraint,
    )


def compute_execution_diagnostics(
    closes_and_volumes: list[tuple[float | None, int | None]],
    *,
    max_position_usd: float | None,
    adv_participation_cap: float,
) -> dict[str, float | int | None]:
    """Return L-6 diagnostics without changing the liquidity gate/sizing path.

    Input is newest first, as used by ``compute_liquidity_profile``.
    """
    dollar_volumes = [float(c) * int(v) for c, v in closes_and_volumes[:ADV_WINDOW_DAYS] if c is not None and v is not None]
    median_adv = median(dollar_volumes) if dollar_volumes else None
    year_values = [float(c) * int(v) for c, v in closes_and_volumes[:252] if c is not None and v is not None]
    stress_pool = sorted(year_values)[: max(1, math.ceil(len(year_values) * 0.10))] if year_values else []
    stress_adv = sum(stress_pool) / len(stress_pool) if stress_pool else None
    zero_days = sum(1 for _, v in closes_and_volumes[:60] if v == 0)
    adv = sum(dollar_volumes) / len(dollar_volumes) if dollar_volumes else None
    denom = adv * adv_participation_cap if adv is not None else None
    stress_denom = stress_adv * adv_participation_cap if stress_adv is not None else None
    return {
        "adv_median_20d": median_adv,
        "adv_stress": stress_adv,
        "zero_volume_days_60d": zero_days,
        "days_to_build": max_position_usd / denom if max_position_usd is not None and denom and denom > 0 else None,
        "days_to_exit_stressed": max_position_usd / stress_denom if max_position_usd is not None and stress_denom and stress_denom > 0 else None,
    }
