"""生データの品質検証層(14.10)。

「取得成功=正しい」ではない、という原則に基づき、スコアリングに使う前に
レンジ検証・クロス検証を行う。検証NGはレコード自体は保存しつつ
`is_valid=False`・`validation_errors`として記録する(データを捨てない)。

**2026-08-24修正**:`is_valid`はraw_snapshotsに記録されるのみでゲート・
スコアリングのどこからも参照されておらず、疑わしい数値がそのまま使われ続けて
いたことが実データレビューで判明した。銘柄まるごとの除外は、Altman
Z''スコアをハードゲートにしなかった教訓(exclusion_gates.py参照:正常な
赤字成長株を誤って弾いた)と同じ問題を再現するリスクがあるため採らない。
代わりに`sanitize_info`で、疑わしいと分かっている特定フィールドだけを
None(欠損)化し、既存の「欠損は除外しない」という設計方針(evaluate_gates
のdocstring)にそのまま委ねる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 粗利率・営業利益率が現実的にとりうる範囲(14.10)。上限1.0は「粗利が売上を
# 超えることはない」という会計上の恒等式、下限は大幅な逆ザヤでも起こりうる
# 極端な赤字企業を許容しつつ、単位エラー(%を小数で二重変換した等)を弾く水準。
_MARGIN_MIN = -5.0
_MARGIN_MAX = 1.0

# marketCap ≒ 終値 × sharesOutstanding の突合で「異常」とみなす倍率。
#
# **2026-08-24修正**:以前は乖離率0.5(±50%)だったが、これは複数種類株
# (デュアルクラス)の銘柄を機械的に異常判定する水準だった。yfinanceの
# `sharesOutstanding` は単一クラス分しか返さないことがある一方 `marketCap` は
# 全クラス合算のため、正常な銘柄でも2倍前後の乖離が普通に発生する。
# `sanitize_info` がこの検知で `marketCap` をNone化し、`evaluate_gates` は
# marketCap欠損を `missing_market_cap` として**除外**するため、創業者主導の
# デュアルクラス小型株(10バガーの典型例)が丸ごとユニバースから消えていた。
# コメントに書かれていた本来の意図どおり「桁違い(単位の取り違え)」だけを
# 捕捉するよう、3倍以上の食い違いに限定する。
_MARKET_CAP_MISMATCH_FACTOR = 3.0

# 前日比較で「単位変更・yfinance側の不具合」を疑う変化倍率(14.10)
_DAY_OVER_DAY_SPIKE_RATIO = 10.0


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: list[str]


def validate_info(info: dict[str, Any]) -> ValidationResult:
    """`Ticker.info` のレンジ検証・クロス検証(14.10)。"""
    errors: list[str] = []

    market_cap = info.get("marketCap")
    shares_outstanding = info.get("sharesOutstanding")
    last_price = info.get("currentPrice") or info.get("regularMarketPrice")

    if market_cap is not None and market_cap < 0:
        errors.append("negative_market_cap")

    for field in ("grossMargins", "operatingMargins"):
        value = info.get(field)
        if value is None:
            continue
        # 13.5: grossMargins=0.0 は「本当に粗利ゼロ」と「未取得(欠損)」の
        # 両方があり得る。売上高が観測できているのに粗利率がちょうど0.0な
        # 場合は欠損の疑いが強いためフラグを立てる(除外はしない)。
        if value == 0.0 and info.get("totalRevenue"):
            errors.append(f"{field}_zero_suspected_missing")
        elif not (_MARGIN_MIN <= value <= _MARGIN_MAX):
            errors.append(f"{field}_out_of_range")

    # クロス検証:時価総額 ≒ 終値 × 発行済株式数(桁違いのみを異常とみなす)
    if market_cap and shares_outstanding and last_price:
        implied_market_cap = last_price * shares_outstanding
        if implied_market_cap > 0 and market_cap > 0:
            ratio = market_cap / implied_market_cap
            if ratio >= _MARKET_CAP_MISMATCH_FACTOR or ratio <= 1 / _MARKET_CAP_MISMATCH_FACTOR:
                errors.append("market_cap_price_shares_mismatch")

    # 通貨の突合(13.5:報告通貨と株価通貨が異なる銘柄が存在する)
    currency = info.get("currency")
    financial_currency = info.get("financialCurrency")
    if currency and financial_currency and currency != financial_currency:
        errors.append("currency_mismatch")

    total_revenue = info.get("totalRevenue")
    if total_revenue is not None and total_revenue < 0:
        errors.append("negative_revenue")

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


# エラーコード -> None化すべきinfoフィールド。「疑わしい派生値(marketCap)を
# 落として、より一次的な観測値(price・sharesOutstanding)は残す」方針。
# currency_mismatchはexclusion_gates.normalize_financial_currency_valueで
# 別途対応済み(除外ではなく換算)なのでここには含めない。operatingMargins は
# 現状どのゲート・サブスコアからも参照されていないため対象外(将来使う時に
# 追加すればよい)。
_NULLABLE_FIELDS_BY_ERROR: dict[str, tuple[str, ...]] = {
    "negative_market_cap": ("marketCap",),
    "market_cap_price_shares_mismatch": ("marketCap",),
    "negative_revenue": ("totalRevenue",),
    "grossMargins_zero_suspected_missing": ("grossMargins",),
}


def sanitize_info(info: dict[str, Any]) -> dict[str, Any]:
    """疑わしいと分かっているフィールドをNone化した`info`のコピーを返す(14.10)。
    銘柄自体は除外せず、既存の「欠損は除外しない」というゲート・スコアリングの
    設計方針(evaluate_gatesのdocstring参照)にそのまま委ねる。

    **2026-08-24修正**:以前は `raw_snapshots.validation_errors`(収集時点で
    保存した判定結果)を引数で受け取っていた。しかし `collect_one` は
    `content_hash` が変わらない限り新しい行を作らないため、**検証ロジックを
    直したあとも古い判定が何ヶ月も残り続ける**(財務データは四半期に一度しか
    変わらない)。実際、marketCapの突合閾値を緩めた直後も、保存済みの
    `market_cap_price_shares_mismatch` 260件がそのまま marketCap をNone化し、
    ゲートで `missing_market_cap` として除外し続けていた。計算に使う
    サニタイズは常に現行ロジックで再導出する(保存側は当時の記録として残す)。"""
    errors = validate_info(info).errors
    if not errors:
        return info
    sanitized = dict(info)
    for error in errors:
        for field in _NULLABLE_FIELDS_BY_ERROR.get(error, ()):
            sanitized[field] = None
    return sanitized


def detect_day_over_day_spike(field: str, previous_value: float | None, current_value: float | None) -> str | None:
    """前日比の急変検知(14.10)。単位変更・yfinance側の不具合の早期検知用。
    異常があればフィールド名を含むエラーコードを返し、無ければ None。"""
    if previous_value is None or current_value is None:
        return None
    if previous_value == 0:
        return None  # ゼロ除算回避。黒字転換等はここでは異常とみなさない
    ratio = current_value / previous_value
    if ratio >= _DAY_OVER_DAY_SPIKE_RATIO or ratio <= 1 / _DAY_OVER_DAY_SPIKE_RATIO:
        return f"{field}_day_over_day_spike"
    return None
