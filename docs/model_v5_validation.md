# Model v5 Validation Decision Record

Machine-readable champion-challenger decision record (Issue #3 section
3.3). One entry per decision review. Appended to, never rewritten in place
-- a later review that reaches a different conclusion adds a new entry
rather than editing this one, so the decision history itself stays
auditable.

**Allowed `decision` values:** `PROMOTE_V5`, `KEEP_V4`, `CONTINUE_SHADOW`,
`UNDETERMINABLE`. No other value is valid. `PROMOTE_V5`/`KEEP_V4` require
comparable, realized-outcome backtest evidence for both models per Issue #3
section 31 -- neither is available as of this entry.

---

## Entry 1 — 2026-09-03 (Phase 7)

```json
{
  "entry": 1,
  "review_date": "2026-09-03",
  "phase": "Phase 7 (backtest / champion-challenger infrastructure)",
  "decision": "CONTINUE_SHADOW",
  "decision_confidence": "high",
  "code_revision": {
    "commit": "6e0bafe60c56a5fe0421d49a7df301cac61cc614",
    "dirty": false
  },
  "reasons": [
    "no_realized_outcome_backtest_available_for_either_model",
    "raw_snapshots_pit_history_spans_9_to_12_days_against_2557_day_target_horizon",
    "v4_own_backtest_effective_evaluation_days_5.31_and_1.21_insufficient_data",
    "v4_delisting_settlement_0_percent_in_backtest_population",
    "v4_coverage_bias_audit_review_required_spearman_0.825",
    "comparing_v5_to_a_broken_v4_baseline_is_explicitly_prohibited_issue_3_section_31"
  ],
  "measured_evidence": {
    "universe_snapshots_included_distinct_dates": 9,
    "universe_snapshots_date_range": ["2026-08-23", "2026-09-02"],
    "raw_snapshots_available_from_range": ["2026-08-23", "2026-09-03"],
    "price_snapshots_trade_date_range": ["1993-01-29", "2026-09-02"],
    "pit_ready_population_overlap_all_9_dates": "100_percent",
    "same_day_comparison_2026_09_02": {
      "v4_population": 1165,
      "v5_population": 1273,
      "overlap_population": 1164,
      "rank_correlation_spearman_v4_probability_vs_v5_ten_bagger": 0.9069484517268488
    },
    "cross_date_ablation_9_historical_mode_runs": {
      "total_ticker_date_pairs": 10099,
      "features_with_nonzero_real_application": [
        "cash_conversion", "accounting_quality", "per_share_economics",
        "incremental_roic", "consensus_revision"
      ],
      "features_with_zero_real_application_all_9_dates": [
        "tam_headroom", "operating_kpi_nowcast", "guidance",
        "reconciliation_confidence", "debt_maturity", "liquidity",
        "capital_allocation", "future_dilution_capacity",
        "customer_concentration", "litigation", "macro_regime"
      ]
    },
    "forward_validation_v5_matured_observations": 0
  },
  "what_would_change_this_decision": [
    "raw_snapshots point-in-time history extending to roughly 7-8 years (a full rebalance-and-horizon cycle at the default 91-day interval against even a 3M interim horizon needs ~2,821 days; the full 7Y target horizon needs 7 years beyond that) accumulating from today forward, with no gaps in universe_snapshots collection",
    "v4's own backtest INSUFFICIENT_DATA / 0% delisting settlement / coverage-bias REVIEW_REQUIRED blockers resolved on an independent track (not a v5 dependency, but a precondition for a meaningful comparison per Issue section 31)",
    "date_block_bootstrap_ci() clearing its 30-distinct-evaluation-date floor for at least an interim horizon",
    "a Phase 9 policy decision on whether interim-horizon (1M-1Y) forward validation may substitute for the full 7Y target horizon during the multi-year accumulation period above"
  ],
  "evidence_docs": [
    "docs/model_v5_phase0_baseline_2026-09-02.md",
    "docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md"
  ],
  "not_claimed": [
    "v5 is more or less accurate than v4",
    "v5's rank correlation with v4 (0.907) implies either model is well-calibrated",
    "any single KPI computed this phase supports promotion or rejection"
  ]
}
```

### Narrative

Phase 7 built the v4-vs-v5 comparison infrastructure required for a future
decision (`backtest/v5_comparison.py`: same-day cross-sectional comparison,
Issue-section-25 PIT enforcement for historical runs, a date-block
bootstrap CI that refuses below a meaningful sample size) and the
forward-validation infrastructure that is the only path to real v5 outcome
evidence (`model_v5_forward_returns`, `run_forward_validation_v5()`). It did
not run an outcome-based backtest comparison, because one is not possible
for either model today: `raw_snapshots` point-in-time history exists only
for a ~9-12 day window against a 7-year target horizon, for both v4 and
v5 alike (v4's own backtest machinery independently confirms this --
its effective evaluation window is a handful of days, per the coordinator's
own KPI figures). Issue #3 section 31 prohibits treating a comparison
against a currently-broken baseline as promotion evidence, so this entry's
decision is `CONTINUE_SHADOW`: v5 remains a shadow challenger, well short
of a promotion review, not because of anything specific to v5's own
implementation quality, but because the data required to evaluate *either*
model's realized outcomes does not exist yet.

The one measurement taken this phase that is not backtest-shaped -- same-day
rank correlation (0.907) between v4's probability and v5's `ten_bagger`
objective score on the current live population -- is recorded as a
structural sanity check (the two models have not diverged into unrelated
rankings) and explicitly not as promotion-relevant evidence; `not_claimed`
above says so directly to prevent a later reader from over-reading it.

---

*Future entries append below this line, in the same JSON + narrative shape,
oldest first.*
