"""将来の希薄化見通し(30.6)。

元文書 第00節の表が指摘するモデルの構造的な穴——「発行済株式数の外挿は過去
実績ベースであり、未使用のシェルフ枠・ATM残枠・転換社債・未行使SOという
**予約済みの希薄化**を一切見ていない」——を、少なくとも**可視化する**。

**自動で埋まる欄と人間が埋める欄の混在になる。** これを曖昧にすると、空欄が
「無い」と誤読される。API/UIは必ず「未入力」(None)と「該当なし」(0や[])を
別の表示にすること(30.6.1)。

純粋関数として実装する(DBに触らない)。呼び出し元(API層)が `filings` から
`FilingRef` を、投資ノートから `dilution:` ブロックを組み立てて渡す。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

SHELF_FORMS = frozenset({"S-3", "S-3ASR"})
OFFERING_FORMS = frozenset({"424B5"})
LOOKBACK_YEARS = 3

# 元文書 第01節「20%を超えるなら7年で10倍という想定に対して重い負担」。
HEAVY_RESERVED_DILUTION_THRESHOLD = 0.20


@dataclass(frozen=True)
class FilingRefView:
    accession_number: str
    form: str
    filed_date: datetime.date
    document_url: str | None


@dataclass(frozen=True)
class NoteDilutionInputs:
    """投資ノートの `dilution:` ブロック(30.7.2)。未入力の項目は None。"""

    remaining_shelf_capacity_usd: float | None = None
    atm_remaining_usd: float | None = None
    unexercised_options_ratio: float | None = None
    has_variable_conversion_price: bool | None = None


@dataclass(frozen=True)
class DilutionOutlookResult:
    shelf_filings: list[FilingRefView]
    offering_filings: list[FilingRefView]
    offerings_last_3y: int
    historical_dilution_rate: float | None
    remaining_shelf_capacity_usd: float | None
    atm_remaining_usd: float | None
    unexercised_options_ratio: float | None
    has_variable_conversion_price: bool | None
    reserved_dilution_ratio: float | None
    heavy_reserved_dilution: bool


def compute_dilution_outlook(
    filings: list[FilingRefView],
    *,
    as_of: datetime.date,
    historical_dilution_rate: float | None,
    market_cap: float | None,
    note: NoteDilutionInputs | None = None,
) -> DilutionOutlookResult:
    cutoff = as_of - datetime.timedelta(days=365 * LOOKBACK_YEARS)
    recent = [f for f in filings if f.filed_date >= cutoff and f.filed_date <= as_of]
    shelf_filings = sorted((f for f in recent if f.form in SHELF_FORMS), key=lambda f: f.filed_date, reverse=True)
    offering_filings = sorted(
        (f for f in recent if f.form in OFFERING_FORMS), key=lambda f: f.filed_date, reverse=True
    )

    note = note or NoteDilutionInputs()
    reserved_amount = None
    if note.remaining_shelf_capacity_usd is not None or note.atm_remaining_usd is not None:
        reserved_amount = (note.remaining_shelf_capacity_usd or 0.0) + (note.atm_remaining_usd or 0.0)

    reserved_dilution_ratio = None
    if reserved_amount is not None and market_cap is not None and market_cap > 0:
        reserved_dilution_ratio = reserved_amount / market_cap

    heavy = reserved_dilution_ratio is not None and reserved_dilution_ratio >= HEAVY_RESERVED_DILUTION_THRESHOLD

    return DilutionOutlookResult(
        shelf_filings=shelf_filings,
        offering_filings=offering_filings,
        offerings_last_3y=len(offering_filings),
        historical_dilution_rate=historical_dilution_rate,
        remaining_shelf_capacity_usd=note.remaining_shelf_capacity_usd,
        atm_remaining_usd=note.atm_remaining_usd,
        unexercised_options_ratio=note.unexercised_options_ratio,
        has_variable_conversion_price=note.has_variable_conversion_price,
        reserved_dilution_ratio=reserved_dilution_ratio,
        heavy_reserved_dilution=heavy,
    )
