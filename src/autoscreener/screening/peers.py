"""Same-date peer selection for display (L-2)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence

@dataclass(frozen=True)
class PeerCandidate:
    ticker: str
    industry: str | None
    sector: str | None
    market_cap: float | None

@dataclass(frozen=True)
class PeerSelection:
    peers: list[PeerCandidate]
    peer_basis: str

def select_peers(target: PeerCandidate, universe: Sequence[PeerCandidate], *, max_peers: int = 10, cap_ratio_range: tuple[float, float] = (0.2, 5.0)) -> PeerSelection:
    def eligible(c: PeerCandidate) -> bool:
        return target.market_cap is not None and target.market_cap > 0 and c.market_cap is not None and cap_ratio_range[0] <= c.market_cap / target.market_cap <= cap_ratio_range[1]
    candidates = [c for c in universe if eligible(c) and target.industry is not None and c.industry == target.industry]
    basis = "industry"
    if len(candidates) < 3:
        candidates = [c for c in universe if eligible(c) and target.sector is not None and c.sector == target.sector]
        basis = "sector"
    if len(candidates) < 3: return PeerSelection([], "none")
    if target not in candidates: candidates.append(target)
    candidates.sort(key=lambda c: c.market_cap or 0, reverse=True)
    selected = candidates[:max_peers]
    if target not in selected: selected[-1] = target
    return PeerSelection(selected, basis)
