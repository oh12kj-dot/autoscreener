"""Canonical daily-pipeline stage order shared by the runner and status API."""

from __future__ import annotations


PIPELINE_STAGE_SEQUENCE = {
    "universe_refresh": 1,
    "cik_map_refresh": 2,
    "macro": 3,
    "xbrl_facts": 4,
    "events": 5,
    "insider": 6,
    "short_interest": 7,
    "collection": 8,
    "consensus": 9,
    "gates": 10,
    "backtest": 11,
    "scoring": 12,
    "forward_validation": 13,
    "filings": 14,
    "filing_sections": 15,
    "guidance": 16,
    "customer_concentration": 17,
    "dilution": 18,
    "litigation": 19,
    "investment_intelligence": 20,
    "market_opportunity": 21,
    "macro_exposure": 22,
    "model_v5_shadow": 23,
    "monitoring": 24,
    "backup": 25,
    # A-4 (2026-09-04, docs/racr_wp_a_operational_safety_2026-09-04.md,
    # audit section 10.1/10.4): wired into daily_pipeline.py's execution
    # list between "model_v5_shadow" and "monitoring". The number stays 26
    # (its former reserved slot below) rather than being renumbered into
    # execution order -- existing stage numbers are never renumbered, so a
    # stage added after two already-numbered stages (24/25) that must run
    # before it in real time necessarily gets a number higher than both.
    # `sequence` therefore reflects *when this number was assigned*, not a
    # strict guarantee of chronological execution order for every stage.
    "forward_validation_v5": 26,
    # P0-A: independent of the price-session stage.  Kept at the next free
    # number; existing persisted sequence values are never renumbered.
    "statement_refresh": 27,
}

PIPELINE_STAGE_COUNT = len(PIPELINE_STAGE_SEQUENCE)

# Stage numbers reserved for code that exists and is real-DB-tested but is
# NOT wired into `daily_pipeline.py`'s actual execution list yet (Phase 7
# audit fix, 2026-09-03): keeping a reserved number inside
# `PIPELINE_STAGE_SEQUENCE` inflated `PIPELINE_STAGE_COUNT` past the number
# of stages the pipeline actually runs, which made
# `frontend/src/pages/PipelinePage.tsx`'s `completedStagesCount /
# expected_stage_count` permanently under-report ("26/27" forever) on every
# future real run -- exactly the kind of silent, always-on false signal
# that page exists to prevent. A reserved number belongs here, in a
# separate constant nothing computes `PIPELINE_STAGE_COUNT` from, until the
# day it is deliberately wired into `PIPELINE_STAGE_SEQUENCE` for real (at
# which point it moves out of this dict and into the sequence with the next
# free number, still never renumbering existing stages).
#
# 2026-09-04 (A-4): empty. `forward_validation_v5` (the only entry this
# dict ever held) was wired in above. Left as an empty dict, not deleted,
# so the next stage that is implemented-but-not-yet-wired has an obvious
# place to go without re-inventing this mechanism.
RESERVED_STAGE_NUMBERS: dict[str, int] = {}
