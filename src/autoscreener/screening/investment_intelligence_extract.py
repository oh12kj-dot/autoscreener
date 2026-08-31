"""Conservative regex extractors for recurring filing disclosures.

Every result includes the exact excerpt. Ambiguous prose is left uncollected;
an LLM may propose candidates later but does not overwrite these observations.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_NUMBER = r"\d[\d,]*(?:\.\d+)?"


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
    }
    found: list[ExtractedValue] = []
    for code, (pattern, unit) in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = _number(match.group(1))
        if value is None:
            continue
        if unit == "ratio": value /= 100
        if unit == "USD" and match.lastindex and match.lastindex >= 2 and match.group(2):
            value *= _SCALE[match.group(2).lower()]
        found.append(ExtractedValue(code, value, unit, match.group(0)[:500]))
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
        results.append(ExtractedDebt(value, int(match.group(3)), match.group(0)[:500]))
    return results


@dataclass(frozen=True)
class ExtractedCapitalEvent:
    event_type: str
    amount: float
    excerpt: str


def extract_capital_events(text: str) -> list[ExtractedCapitalEvent]:
    terms = {"repurchase": "buyback", "acquisition": "acquisition", "dividend": "dividend", "capital expenditure": "capex"}
    results: list[ExtractedCapitalEvent] = []
    for term, event_type in terms.items():
        pattern = re.compile(rf"\b{term}\w*\b[^$]{{0,100}}\$({_NUMBER})\s*(thousand|million|billion)?", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            number = _number(match.group(1))
            if number is None:
                continue
            amount = number * _SCALE.get((match.group(2) or "").lower(), 1)
            results.append(ExtractedCapitalEvent(event_type, amount, match.group(0)[:500]))
    return results


def extract_beneficial_ownership(text: str) -> float | None:
    match = re.search(rf"beneficial(?:ly)?\s+own\w*[^%]{{0,120}}({_NUMBER})\s*%", text, re.IGNORECASE)
    value = _number(match.group(1)) if match else None
    return value / 100 if value is not None else None
