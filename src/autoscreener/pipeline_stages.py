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
RESERVED_STAGE_NUMBERS = {
    # Phase 7 (Issue #3 27章): run_forward_validation_v5() and the
    # `run-forward-validation-v5` CLI command are implemented and
    # real-DB-tested; only the daily_pipeline.py wiring is deferred (see
    # docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md
    # "Deviations").
    "forward_validation_v5": 26,
}
