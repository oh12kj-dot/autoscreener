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
    "monitoring": 21,
    "backup": 22,
}

PIPELINE_STAGE_COUNT = len(PIPELINE_STAGE_SEQUENCE)
