"""SEC companyfacts JSON から4概念(売上・現金・負債・株式数)を抽出する
(30.5.1)。純粋関数(DBに触らない)。

**フォールバック順を持つのが要点。** 単一タグ決め打ちだと、収益認識基準の
適用時期によってタグが変わる銘柄で軒並み欠損する。最初に値が取れたタグを
使い、どのタグを使ったかを保存する(後から「なぜこの値になったか」を
追えるようにするため)。
"""

from __future__ import annotations

import datetime

# 概念 → (タクソノミ, タグ, 単位)の優先順リスト。最初に値が取れたタグを使う。
#
# B-3(defect_and_edge_audit_2026-08-28.md I-1):元は4概念だけで、用途は
# yfinance との突合に限られていた(モデルは1バイトも使っていなかった)。
# `MoicInputs` の全フィールドを `filed` 日付で切った真のポイントインタイム値から
# 組み立てられるよう、大幅に拡張する。合算が要る概念(cash + 短期投資、
# 有利子負債の各成分、リース債務の流動/非流動)は成分ごとにタグを持ち、
# `scoring.point_in_time_xbrl` 側で足し合わせる。
CONCEPT_TAGS: dict[str, list[tuple[str, str, str]]] = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
        ("us-gaap", "Revenues", "USD"),
        ("us-gaap", "SalesRevenueNet", "USD"),
    ],
    "cost_of_revenue": [
        ("us-gaap", "CostOfRevenue", "USD"),
        ("us-gaap", "CostOfGoodsAndServicesSold", "USD"),
    ],
    "gross_profit": [
        ("us-gaap", "GrossProfit", "USD"),
    ],
    "net_income": [
        ("us-gaap", "NetIncomeLoss", "USD"),
    ],
    "operating_cash_flow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "USD"),
    ],
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets", "USD"),
    ],
    "assets": [
        ("us-gaap", "Assets", "USD"),
    ],
    "liabilities": [
        ("us-gaap", "Liabilities", "USD"),
    ],
    "equity": [
        ("us-gaap", "StockholdersEquity", "USD"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
    ],
    "cash": [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
    ],
    "short_term_investments": [
        ("us-gaap", "ShortTermInvestments", "USD"),
    ],
    "long_term_debt_noncurrent": [
        ("us-gaap", "LongTermDebtNoncurrent", "USD"),
    ],
    "long_term_debt_current": [
        ("us-gaap", "LongTermDebtCurrent", "USD"),
    ],
    "short_term_borrowings": [
        ("us-gaap", "ShortTermBorrowings", "USD"),
    ],
    "operating_lease_liability_noncurrent": [
        ("us-gaap", "OperatingLeaseLiabilityNoncurrent", "USD"),
    ],
    "operating_lease_liability_current": [
        ("us-gaap", "OperatingLeaseLiabilityCurrent", "USD"),
    ],
    "current_assets": [
        ("us-gaap", "AssetsCurrent", "USD"),
    ],
    "current_liabilities": [
        ("us-gaap", "LiabilitiesCurrent", "USD"),
    ],
    "shares_outstanding": [
        ("dei", "EntityCommonStockSharesOutstanding", "shares"),
    ],
    "diluted_shares": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
    ],
    "public_float": [
        ("dei", "EntityPublicFloat", "USD"),
    ],
    "r_and_d": [
        ("us-gaap", "ResearchAndDevelopmentExpense", "USD"),
    ],
}

# B-3:突合(`validation/reconciliation.py`)がこれまで扱っていた4概念。
# 拡張した概念は yfinance 側に対応する値が無いことが多いので、突合の
# 対象はこの4つに限る(新概念はモデル入力の再構成専用)。
RECONCILIATION_CONCEPTS = ("revenue", "cash", "liabilities", "shares_outstanding")


class ExtractedFact:
    __slots__ = ("concept", "taxonomy", "tag", "unit", "period_start", "period_end", "value", "form",
                 "accession_number", "filed_date", "fiscal_year", "fiscal_period")

    def __init__(
        self, concept, taxonomy, tag, unit, period_start, period_end, value, form,
        accession_number, filed_date, fiscal_year, fiscal_period,
    ) -> None:
        self.concept = concept
        self.taxonomy = taxonomy
        self.tag = tag
        self.unit = unit
        self.period_start = period_start
        self.period_end = period_end
        self.value = value
        self.form = form
        self.accession_number = accession_number
        self.filed_date = filed_date
        self.fiscal_year = fiscal_year
        self.fiscal_period = fiscal_period


def _extract_tag_facts(
    company_facts: dict, taxonomy: str, tag: str, unit: str
) -> list[dict]:
    facts_by_taxonomy = company_facts.get("facts") or {}
    tag_data = (facts_by_taxonomy.get(taxonomy) or {}).get(tag)
    if not tag_data:
        return []
    units = tag_data.get("units") or {}
    return units.get(unit) or []


def extract_concept_facts(company_facts: dict, concept: str) -> list[ExtractedFact]:
    """1概念ぶん、フォールバックタグを順に試し、**最初に値が取れたタグ**の
    全期間分の観測を返す(1タグのみ採用。複数タグを混在させない)。
    """
    for taxonomy, tag, unit in CONCEPT_TAGS[concept]:
        raw_facts = _extract_tag_facts(company_facts, taxonomy, tag, unit)
        if not raw_facts:
            continue
        extracted: list[ExtractedFact] = []
        for item in raw_facts:
            try:
                period_end = datetime.date.fromisoformat(item["end"])
                filed_date = datetime.date.fromisoformat(item["filed"])
                value = float(item["val"])
            except (KeyError, ValueError, TypeError):
                continue
            period_start = None
            if item.get("start"):
                try:
                    period_start = datetime.date.fromisoformat(item["start"])
                except ValueError:
                    pass
            extracted.append(
                ExtractedFact(
                    concept=concept,
                    taxonomy=taxonomy,
                    tag=tag,
                    unit=unit,
                    period_start=period_start,
                    period_end=period_end,
                    value=value,
                    form=item.get("form", ""),
                    accession_number=item.get("accn", ""),
                    filed_date=filed_date,
                    fiscal_year=item.get("fy"),
                    fiscal_period=item.get("fp"),
                )
            )
        if extracted:
            # このタグで1件以上取れたら採用し、他のフォールバックタグは試さない
            # (30.5.1:複数タグを混在させると「どのタグの値か」が追えなくなる)。
            return extracted
    return []


def extract_all_concepts(company_facts: dict) -> dict[str, list[ExtractedFact]]:
    return {concept: extract_concept_facts(company_facts, concept) for concept in CONCEPT_TAGS}


# タグ名 → 概念名の逆引き。`xbrl_facts` テーブルは `concept` 列を持たない
# (どのタグを採用したかだけを保存する、30.5.1)ため、読み出し側(突合)で
# 概念に戻すのに使う。フォールバックタグは概念間で重複しない前提。
_TAG_TO_CONCEPT: dict[tuple[str, str], str] = {
    (taxonomy, tag): concept
    for concept, entries in CONCEPT_TAGS.items()
    for taxonomy, tag, _unit in entries
}


def tag_to_concept(taxonomy: str, tag: str) -> str | None:
    return _TAG_TO_CONCEPT.get((taxonomy, tag))
