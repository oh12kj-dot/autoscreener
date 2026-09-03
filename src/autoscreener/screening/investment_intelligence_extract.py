"""Conservative regex extractors for recurring filing disclosures.

Every result includes the exact excerpt. Ambiguous prose is left uncollected;
an LLM may propose candidates later but does not overwrite these observations.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
# SEC inline-XBRL tables can lose cell boundaries during text extraction and
# concatenate several monetary columns into one enormous number.  No single
# company debt principal in this screener should approach a quadrillion USD;
# rejecting it is safer than persisting a fabricated value (or overflowing the
# database NUMERIC column).
_MAX_PLAUSIBLE_USD = 1e15


def _number(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


@dataclass(frozen=True)
class ExtractedValue:
    code: str
    value: float
    unit: str
    excerpt: str


def extract_operating_kpis(text: str) -> list[ExtractedValue]:
    patterns = {
        "arr": (rf"\b(?:annual recurring revenue|ARR)\b[^$]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", "USD"),
        "nrr": (rf"\b(?:net revenue retention|net dollar retention|NRR)\b[^\d]{{0,50}}({_NUMBER})\s*%", "ratio"),
        "backlog": (rf"\bbacklog\b[^$]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", "USD"),
        "customer_count": (rf"\b({_NUMBER})\s+(?:customers|active customers)\b", "count"),
        "rpo": (rf"\b(?:remaining performance obligations|RPO)\b[^$]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", "USD"),
        "gmv": (rf"\b(?:gross merchandise value|GMV)\b[^$]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", "USD"),
        "tpv": (rf"\b(?:total payment volume|TPV)\b[^$]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", "USD"),
        "take_rate": (rf"\btake rate\b[^\d]{{0,50}}({_NUMBER})\s*%", "ratio"),
        "store_count": (rf"\b({_NUMBER})\s+(?:stores|locations)\b", "count"),
        "book_to_bill": (rf"\bbook[- ]to[- ]bill\b[^\d]{{0,50}}({_NUMBER}(?:\.\d+)?)", "ratio"),
        "production": (rf"\bproduction\b[^\d]{{0,80}}({_NUMBER})\s*(?:barrels|boe|tons|ounces)\b", "count"),
    }
    found: list[ExtractedValue] = []
    for code, (pattern, unit) in patterns.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = _number(match.group(1))
            if value is None:
                continue
            if unit == "ratio": value /= 100
            if unit == "USD" and match.lastindex and match.lastindex >= 2 and match.group(2):
                value *= _SCALE[match.group(2).lower()]
            excerpt = match.group(0)[:500]
            # A metric qualified as market-wide or a competitor's is not a
            # company operating KPI.  Leave it for human research instead.
            context = text[max(0, match.start() - 80):match.end() + 80].lower()
            if any(term in context for term in ("market size", "competitor", "industry-wide", "forecast")):
                continue
            found.append(ExtractedValue(code, value, unit, excerpt))
    return found


@dataclass(frozen=True)
class ExtractedDebt:
    principal: float
    maturity_year: int
    excerpt: str


def extract_debt_maturities(text: str) -> list[ExtractedDebt]:
    pattern = re.compile(
        rf"\$({_NUMBER})\s*(thousand|million|billion)?[^.\n]{{0,100}}?\b(?:due|matur(?:e|es|ing|ity))\b[^.\n]{{0,50}}?\b(20\d{{2}})\b",
        re.IGNORECASE,
    )
    results: list[ExtractedDebt] = []
    for match in pattern.finditer(text):
        number = _number(match.group(1))
        if number is None:
            continue
        value = number * _SCALE.get((match.group(2) or "").lower(), 1)
        if value <= 0 or value > _MAX_PLAUSIBLE_USD:
            continue
        results.append(ExtractedDebt(value, int(match.group(3)), match.group(0)[:500]))
    table_pattern = re.compile(rf"\b(20\d{{2}})\b[^\n]{{0,80}}\$({_NUMBER})\s*(thousand|million|billion)?", re.IGNORECASE)
    for match in table_pattern.finditer(text):
        number = _number(match.group(2))
        if number is None:
            continue
        value = number * _SCALE.get((match.group(3) or "").lower(), 1)
        if value <= 0 or value > _MAX_PLAUSIBLE_USD:
            continue
        result = ExtractedDebt(value, int(match.group(1)), match.group(0)[:500])
        if result not in results:
            results.append(result)
    return results


@dataclass(frozen=True)
class ExtractedCapitalEvent:
    event_type: str
    amount: float
    excerpt: str


def extract_capital_events(text: str) -> list[ExtractedCapitalEvent]:
    terms = {
        "repurchase": "buyback", "buyback": "buyback", "acquisition": "acquisition",
        "divestiture": "divestiture", "divested": "divestiture", "dividend": "dividend",
        "capital expenditure": "capex", "capital expenditures": "capex",
        "registered direct offering": "equity_raise", "public offering": "equity_raise",
        "common stock offering": "equity_raise", "senior notes offering": "debt_raise",
        "debt financing": "debt_raise",
    }
    results: list[ExtractedCapitalEvent] = []
    money_re = re.compile(rf"\$({_NUMBER})\s*(thousand|million|billion)?", re.IGNORECASE)
    # Associate a term with the closest disclosed amount in the same sentence.
    # A broad `term ... $amount` regex incorrectly assigns the *next* deal's
    # amount to an earlier repurchase when a sentence contains multiple facts.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        for term, event_type in terms.items():
            pattern = re.compile(rf"\b{re.escape(term)}\w*\b", re.IGNORECASE)
            for match in pattern.finditer(sentence):
                before = list(money_re.finditer(sentence[:match.start()]))
                after = money_re.search(sentence[match.end():])
                amount_match = before[-1] if before else after
                if amount_match is None:
                    continue
                number = _number(amount_match.group(1))
                if number is None:
                    continue
                amount = number * _SCALE.get((amount_match.group(2) or "").lower(), 1)
                results.append(ExtractedCapitalEvent(event_type, amount, sentence[:500]))
    return results


@dataclass(frozen=True)
class ExtractedLiquidity:
    cash_balance: float | None
    revolver_total: float | None
    revolver_drawn: float | None
    revolver_available: float | None
    atm_remaining: float | None
    shelf_remaining: float | None
    excerpt: str


def extract_liquidity_facilities(text: str) -> list[ExtractedLiquidity]:
    """Conservative filing-text liquidity facts; unknown values remain None."""
    results: list[ExtractedLiquidity] = []
    for match in re.finditer(rf"\$({_NUMBER})\s*(thousand|million|billion)?[^.\n]{{0,120}}\b(?:revolving credit facility|revolver|credit facility)\b[^.\n]{{0,160}}", text, re.IGNORECASE):
        total = _number(match.group(1))
        total = total * _SCALE.get((match.group(2) or "").lower(), 1) if total is not None else None
        excerpt = match.group(0)[:500]
        available_match = re.search(rf"\$({_NUMBER})\s*(thousand|million|billion)?\s+(?:available|remaining)", excerpt, re.IGNORECASE)
        available = _number(available_match.group(1)) if available_match else None
        if available is not None:
            available *= _SCALE.get((available_match.group(2) or "").lower(), 1)
        results.append(ExtractedLiquidity(None, total, None, available, None, None, excerpt))
    return results


def extract_beneficial_ownership(text: str) -> float | None:
    match = re.search(rf"beneficial(?:ly)?\s+own\w*[^%]{{0,120}}({_NUMBER})\s*%", text, re.IGNORECASE)
    value = _number(match.group(1)) if match else None
    return value / 100 if value is not None else None


@dataclass(frozen=True)
class ExtractedManagementIncentive:
    executive_name: str
    role: str
    total_compensation: float | None
    equity_compensation_pct: float | None
    ownership_pct: float | None
    excerpt: str


def extract_management_incentives(text: str) -> list[ExtractedManagementIncentive]:
    """Parse explicit named-executive lines; never promote shareholder rows."""
    role_re = r"(?:chief executive officer|CEO|chief financial officer|CFO|president|chief operating officer|COO)"
    pattern = re.compile(rf"\b([A-Z][a-z]+(?:\s+[A-Z][a-z.'-]+){{1,3}})\b[^\n.]{{0,100}}\b({role_re})\b[^\n.]{{0,180}}?\$({_NUMBER})\s*(thousand|million)?", re.IGNORECASE)
    results: list[ExtractedManagementIncentive] = []
    for match in pattern.finditer(text):
        total = _number(match.group(3))
        if total is None:
            continue
        total *= _SCALE.get((match.group(4) or "").lower(), 1)
        excerpt = match.group(0)[:500]
        ownership_match = re.search(rf"({_NUMBER})\s*%[^.\n]{{0,50}}beneficial", excerpt, re.IGNORECASE)
        ownership = _number(ownership_match.group(1)) / 100 if ownership_match else None
        results.append(ExtractedManagementIncentive(match.group(1), match.group(2), total, None, ownership, excerpt))
    return results
