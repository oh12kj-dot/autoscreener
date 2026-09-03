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

## Entry 2 — 2026-09-03 (Phase 8 UI + Phase 9 promotion-decision infrastructure)

```json
{
  "entry": 2,
  "review_date": "2026-09-03",
  "phase": "Phase 8 (frontend UI) + Phase 9 (promotion-decision infrastructure)",
  "decision": "CONTINUE_SHADOW",
  "decision_confidence": "high",
  "code_revision": {
    "commit": "64f185bd18e69a2c53d918bd64f907f0828d8be8",
    "dirty": false
  },
  "reasons": [
    "no_new_realized_outcome_evidence_was_created_this_phase_phase_8_9_are_ui_and_infrastructure_only",
    "entry_1s_reasons_are_unchanged_and_still_apply",
    "forward_validation_v5_matured_observations_still_0"
  ],
  "measured_evidence": {
    "backend_tests_passed": 941,
    "frontend_tests_passed": 2,
    "frontend_production_build": "succeeded",
    "frontend_lint_errors": 0,
    "v5_evidence_run_2026_09_02": {
      "population": 1273,
      "input_ready": 1215,
      "base_distributions": 1164,
      "objective_scores": 6365,
      "ablation_results": 2568,
      "enabled_objectives": ["asymmetric", "capital_preservation", "expected_return", "risk_adjusted", "ten_bagger"]
    },
    "validation_status_endpoint_live_read": {
      "evaluation_dates_count": 9,
      "evaluation_date_range": ["2026-08-23", "2026-09-02"],
      "realized_forward_validation_count": 0,
      "unsupported_historical_features": ["acquisition_competing_risk", "litigation", "macro_regime"]
    },
    "rollback_path_verified_live": {
      "model_runs_count_before": 29,
      "enabled_false_override_result": {"status": "skipped", "reason": "disabled_by_config"},
      "model_runs_count_after_enabled_false": 29,
      "v4_scores_count_before": 8225,
      "v4_scores_count_after_enabled_false": 8225,
      "mode_live_override_result": "raised ValueError('v5 shadow runner requires mode=shadow')",
      "model_runs_count_after_mode_override": 29,
      "config_file_config_model_v5_yaml_modified_on_disk": false,
      "note": "verified via ModelV5Config.model_copy(update=...) in-process overrides, not by editing the real config/model_v5.yaml file, to avoid any window where the real config diverges from what the (unmodified) 09:00 JST batch would read"
    },
    "migration_reversibility_verified_via_offline_sql": {
      "method": "alembic downgrade --sql / upgrade --sql (offline SQL generation, not executed against the shared dev DB, to avoid destroying the accumulated v5 run history that DB also holds)",
      "revision_chain": "e9b1c3d5f7a9 -> f0a1b2c3d4e5 -> 1d2e3f4a5b6c -> 2c4e6f8a1b3d (head)",
      "downgrade_sql_touches_only": ["model_runs", "model_scores", "objective_scores", "model_v5_forward_returns", "alembic_version"],
      "no_v4_table_referenced_in_generated_sql": true
    }
  },
  "what_would_change_this_decision": [
    "same as Entry 1 -- nothing in Phase 8/9 changes the underlying realized-outcome-evidence gap"
  ],
  "evidence_docs": [
    "docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md",
    "docs/model_v5_phase8_ui_2026-09-03.md"
  ],
  "not_claimed": [
    "v5 is ready for production use",
    "the UI work changes the evidence available for a PROMOTE_V5/KEEP_V4 decision",
    "the rollback and migration checks above were run against a full production-scale rehearsal -- they confirm code-path and SQL correctness, not operational runbook execution"
  ]
}
```

### Narrative

Phase 8 added a v5 surface to the frontend (model/objective selector on
`RankingPage`, ablation-based state-shift explanation and a v4-vs-v5
comparison table on `TickerDetailPage`, and a champion/challenger
validation-status section on `ValidationPage`) with zero changes to any
existing v4 rendering path, and two new read-only backend endpoints
(`/models/v5/objectives`, `/models/v5/validation-status`). Phase 9 verified
the promotion-decision infrastructure itself rather than producing new
model evidence: the rollback path (`config/model_v5.yaml` `enabled: false`
writes zero rows and leaves `model_runs` and v4's `scores` table counts
unchanged; requesting a non-`shadow` `mode` raises before any write) was
exercised live against the real database, and migration reversibility was
confirmed by generating (not executing) the offline downgrade/upgrade SQL
for the full v5 migration chain, showing it only ever creates or drops the
four v5-specific tables and never references any v4 table.

None of this changes Entry 1's central finding: no realized-outcome
backtest exists for either model yet, so `PROMOTE_V5`/`KEEP_V4` remain
unavailable per Issue #3 section 31. This entry exists to record that the
UI and rollback/migration infrastructure pieces of Issue #3 section 36's
Definition of Done are now in place and independently verified, distinct
from the still-open realized-outcome-evidence gap that section 31 gates on.
See the Definition-of-Done item-by-item assessment in
[model_v5_phase8_ui_2026-09-03.md](model_v5_phase8_ui_2026-09-03.md) for
which of section 36's 22 checklist items are achieved, not yet achieved,
or structurally blocked and why.

---

*Future entries append below this line, in the same JSON + narrative shape,
oldest first.*
