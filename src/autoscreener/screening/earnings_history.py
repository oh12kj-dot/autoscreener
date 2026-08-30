"""Analyst estimate and earnings-surprise history (L-3)."""
from __future__ import annotations
from dataclasses import dataclass, field
from statistics import median


@dataclass(frozen=True)
class EarningsPeriod:
    date: str
    estimate: float | None
    reported: float | None
    surprise_pct: float | None


@dataclass(frozen=True)
class EarningsHistory:
    covered: bool
    analyst_count: int | None
    periods: list[EarningsPeriod] = field(default_factory=list)
    beat_count_8q: int | None = None
    miss_count_8q: int | None = None
    mean_surprise_pct_8q: float | None = None
    median_surprise_pct_8q: float | None = None
    consecutive_beats: int | None = None
    estimate_revision_30d: float | None = None
    estimate_revision_7d: float | None = None
    next_estimate: float | None = None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def build_earnings_history(payload: dict) -> EarningsHistory:
    info = payload.get("info") or {}
    rows = payload.get("earnings_dates") or []
    if not isinstance(rows, list): rows = []
    analyst_count = _number(info.get("numberOfAnalystOpinions"))
    analyst_count = int(analyst_count) if analyst_count is not None else None
    covered = bool(rows) or analyst_count is not None
    actual: list[EarningsPeriod] = []
    future: list[tuple[str, float | None]] = []
    for row in rows:
        if not isinstance(row, dict): continue
        estimate, reported = _number(row.get("eps_estimate")), _number(row.get("reported_eps"))
        date = str(row.get("date") or "")
        if reported is None:
            future.append((date, estimate)); continue
        surprise = (reported - estimate) / abs(estimate) if estimate is not None and estimate > 0 else None
        actual.append(EarningsPeriod(date, estimate, reported, surprise))
    actual.sort(key=lambda x: x.date, reverse=True)
    periods = actual[:8]
    surprises = [p.surprise_pct for p in periods if p.surprise_pct is not None]
    beats = [p for p in periods if p.surprise_pct is not None and p.surprise_pct > 0]
    misses = [p for p in periods if p.surprise_pct is not None and p.surprise_pct < 0]
    consecutive = 0
    for period in periods:
        if period.surprise_pct is not None and period.surprise_pct > 0: consecutive += 1
        else: break
    revisions = payload.get("eps_revisions") or {}
    def revision(days: int) -> float | None:
        if not isinstance(revisions, dict): return None
        keys = (f"upLast{days}days", f"up_last_{days}d", f"up_{days}d")
        up = next((_number(revisions.get(k)) for k in keys if _number(revisions.get(k)) is not None), None)
        down_keys = (f"downLast{days}days", f"down_last_{days}d", f"down_{days}d")
        down = next((_number(revisions.get(k)) for k in down_keys if _number(revisions.get(k)) is not None), None)
        return (up - down) / (up + down) if up is not None and down is not None and up + down > 0 else None
    future.sort(key=lambda x: x[0])
    return EarningsHistory(covered, analyst_count, periods, len(beats) if periods else None, len(misses) if periods else None,
        sum(surprises)/len(surprises) if surprises else None, median(surprises) if surprises else None,
        consecutive if periods else None, revision(30), revision(7), future[0][1] if future else None)
