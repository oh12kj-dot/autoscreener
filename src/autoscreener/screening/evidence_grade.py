"""Data-support explanation, deliberately independent of scoring (L-7)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Sequence

@dataclass(frozen=True)
class EvidenceGrade:
    grade: str
    clamp_count: int
    missing_count: int
    reconciliation_mismatch_count: int
    period_count: int
    reasons: list[str] = field(default_factory=list)

def compute_evidence_grade(*, warnings: Sequence[str], reconciliation: Sequence[object], annual_period_count: int, quarterly_period_count: int, data_age_days: int | None) -> EvidenceGrade:
    clamp = sum("clamp" in str(w).lower() for w in warnings)
    missing = sum("missing" in str(w).lower() or "unavailable" in str(w).lower() for w in warnings)
    mismatch = sum(getattr(item, "status", None) in {"mismatch", "magnitude_mismatch"} for item in reconciliation)
    reasons = ([f"モデル入力の上限・下限クランプ: {clamp}件"] if clamp else []) + ([f"欠損・未取得の警告: {missing}件"] if missing else []) + ([f"SEC突合の不一致: {mismatch}件"] if mismatch else [])
    periods = annual_period_count + quarterly_period_count
    if periods < 3: reasons.append("財務履歴が3期未満")
    if data_age_days is not None and data_age_days > 10: reasons.append(f"価格データが{data_age_days}営業日古い")
    penalty = clamp + missing + mismatch + (1 if periods < 3 else 0) + (1 if data_age_days is not None and data_age_days > 10 else 0)
    return EvidenceGrade("A" if penalty == 0 else "B" if penalty <= 2 else "C", clamp, missing, mismatch, periods, reasons)
