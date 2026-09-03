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
    # Phase 7 (Issue #3 27章): appended at the end per the handoff's explicit
    # instruction not to renumber existing stages. The number is reserved
    # here so `PIPELINE_STAGE_SEQUENCE`/`PIPELINE_STAGE_COUNT` stay the
    # single source of truth for stage ordering, but this stage is NOT
    # wired into daily_pipeline.py's actual execution list in this phase --
    # a fresh, real-DB-unvalidated-in-production code path should not be
    # spliced into the live 09:00 JST scheduled pipeline as a side effect of
    # an infrastructure/measurement deliverable. Run manually via
    # `run-forward-validation-v5` until a deliberate follow-up wires it in.
    "forward_validation_v5": 26,
}

PIPELINE_STAGE_COUNT = len(PIPELINE_STAGE_SEQUENCE)
