"""Classification-only valuation model router (no ranking side effects)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ValuationModel(Protocol):
    name: str
    version: str
    def supports(self, profile: "CompanyModelProfile") -> bool: ...
    def build_inputs(self, *args, **kwargs): ...
    def score(self, *args, **kwargs): ...


@dataclass(frozen=True)
class CompanyModelProfile:
    sector: str | None = None
    industry: str | None = None
    revenue: float | None = None
    research_and_development: float | None = None
    has_ffo: bool = False


@dataclass(frozen=True)
class ModelRoute:
    model_family: str
    supported: bool
    reason: str


def classify_model_family(profile: CompanyModelProfile) -> ModelRoute:
    sector = (profile.sector or "").lower()
    industry = (profile.industry or "").lower()
    text = f"{sector} {industry}"
    if "insurance" in text:
        family = "insurance"
    elif any(term in text for term in ("bank", "banks", "financial services")):
        family = "bank"
    elif "reit" in text or ("real estate" in text and profile.has_ffo):
        family = "reit"
    elif any(term in text for term in ("gold", "copper", "oil & gas", "mining", "commodity")):
        family = "commodity_producer"
    elif "biotech" in text and (profile.revenue is None or profile.revenue <= 0):
        family = "biotech_pre_revenue"
    elif sector or industry:
        family = "general_corporate"
    else:
        family = "unclassified"
    supported = family == "general_corporate"
    return ModelRoute(family, supported, "current_moic_model" if supported else "dedicated_model_not_available")
