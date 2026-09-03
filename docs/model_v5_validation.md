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

## Entry 3 -- 2026-09-03 (Phase 10: objective-layer distribution/uncertainty fixes)

```json
{
  "entry": 3,
  "review_date": "2026-09-03",
  "phase": "Phase 10 (risk_adjusted/ten_bagger objective-layer methodology fixes)",
  "decision": "CONTINUE_SHADOW",
  "decision_confidence": "high",
  "code_revision": {
    "commit": "3c48fe3ce0cf3d00af3fd56d60bdfa066b7219b1",
    "dirty": false
  },
  "reasons": [
    "no_new_realized_outcome_evidence_was_created_this_phase_phase_10_is_objective_layer_methodology_only",
    "entry_1s_central_finding_is_unchanged_no_realized_outcome_backtest_exists_for_either_model",
    "these_fixes_change_how_v5s_own_objectives_rank_tickers_relative_to_each_other_not_whether_v5_vs_v4_comparison_evidence_exists"
  ],
  "measured_evidence": {
    "finding_1_risk_adjusted_was_a_constant_shift_of_expected_return": {
      "root_cause": "expected_shortfall_10pct collapses to exactly 0.0 whenever failure_mass >= 10pct, true for 100pct of the real universe (1164/1164 measured twice)",
      "before_fix": "risk_adjusted == expected_cagr - lambda, a pure constant shift -- Spearman(expected_return, risk_adjusted) == 1.0 by algebraic identity for any lambda",
      "after_fix_real_persisted_run_893b8386": {
        "spearman_expected_return_vs_risk_adjusted": 0.9919649725206005,
        "top20_overlap_count": 18,
        "top20_total": 20,
        "expected_moic_given_loss_defined_count": 1164,
        "expected_moic_given_loss_distinct_values": 1160,
        "expected_moic_given_loss_range": [0.0069478229684922016, 0.4188054156318529]
      }
    },
    "finding_2_ten_bagger_rewarded_reliability_driven_widening": {
      "before_fix_real_isolated_ablation_712_tickers": {
        "delta_positive_count": 712,
        "delta_positive_total": 712,
        "mean_delta_severity_gte_0_5": 0.003669035240914839,
        "mean_delta_severity_lt_0_5": 0.0018183705666688934,
        "spearman_severity_vs_delta": 0.2661883517288287
      },
      "chosen_config": {"reliability_sigma_lambda": 20.0, "reliability_left_tail_lambda": 20.0},
      "after_fix_real_isolated_ablation_712_tickers": {
        "delta_nonpositive_count": 661,
        "delta_nonpositive_total": 712,
        "mean_delta_severity_gte_0_5": -0.002526995709262884,
        "mean_delta_severity_lt_0_5": -0.002780626124068923,
        "spearman_severity_vs_delta": 0.12978529004901432
      },
      "population_level_full_universe_1164_tickers": {
        "spearman_severity_vs_ten_bagger_rank_before_fix": -0.006755723569447428,
        "spearman_severity_vs_ten_bagger_rank_after_fix": 0.22962297115275157,
        "interpretation": "positive after the fix means worse severity now correlates with a worse (larger-numbered) rank, the economically correct direction"
      },
      "mean_preservation_check": "widened distribution (sigma_multiplier=1.5, left_tail_extra=0.35) expected_moic and expected_cagr equal the clean distribution's to within pytest.approx rel=1e-9 -- Issue section 6.3 not violated"
    }
  },
  "dod_item_4_correction": {
    "prior_status_in_phase8_doc": "not achieved (based on a stale 2026-09-01 memory note)",
    "corrected_status": "achieved -- verified fixed in commit 7f057906a6fc5f2f1d25c40d89061fdd9a415fad (Phase 0, predates this session) plus fresh real-DB re-verification this round (delisting_events: 94/94 unknown, correctly excluded from acquisition-rate denominator; market_opportunity_estimate: 4/4 rows with plausible magnitudes, zero bare-number artifacts)",
    "separate_still_open_concern_not_conflated_with_item_4": "TAM extraction coverage remains very low (4 rows across ~1273 tickers) -- a coverage problem, not a correctness problem; fixing requires large-scale external API access not authorized this round"
  },
  "dod_item_19_status": "unchanged, not attempted, explicit coordinator instruction (would require ~4.7 hour production pipeline run and large-scale external API access)",
  "tests": {"backend_passed": 952, "new_this_phase": 11, "prior_baseline": 941, "frontend_passed": 2, "frontend_build": "succeeded", "frontend_lint_errors": 0},
  "evidence_docs": [
    "docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md",
    "docs/model_v5_phase8_ui_2026-09-03.md",
    "docs/model_v5_phase10_reliability_objectives_2026-09-03.md"
  ],
  "not_claimed": [
    "v5 is ready for production use",
    "risk_adjusted is now a strongly independent risk measure from expected_return (0.992 is still high; only the exact 1.0 identity is what was fixed)",
    "the reliability-widening discount eliminates every individual case where worse severity nominally increases ten_bagger (93pct of isolated per-ticker comparisons flip; ~7pct residual explained by extreme-tail lognormal sensitivity, honestly reported not tuned away)",
    "this phase changes anything about scenario.py/quality.py/tail_risk.py -- both fixes are objectives.py-layer plus two new diagnostic distribution fields",
    "promotion evidence changed -- no realized-outcome data was created this phase"
  ]
}
```

### Narrative

Phase 10 fixed two methodology defects the coordinator found by real-data
ablation and severity analysis, neither of which any unit test had caught
(the full Phase 2-9 suite passed throughout, both before and after this
phase's changes -- exactly why a real-data audit was needed).

First, `risk_adjusted` was proven to be an exact constant shift of
`expected_return` -- not approximately correlated, but algebraically
identical in rank order -- because its downside input
(`expected_shortfall_10pct`) is defined at a fixed *probability level*
that collapses to 0.0 for the entire real universe once failure mass
exceeds 10%, which it does for every ticker measured. Replacing that input
with a fixed-*cutoff* conditional expectation (`expected_moic_given_loss`,
`E[MOIC | MOIC < 1.0]`) breaks the identity: measured
`Spearman(expected_return, risk_adjusted) = 0.992` (down from exactly
1.0) with a real, differing top-20 roster (18/20 overlap) on the same
2026-09-02 universe.

Second, `ten_bagger` mechanically rewarded a reliability/quality-driven
widening of the distribution -- confirmed in 712/712 real
`accounting_quality` ablations, and traced to a genuine mathematical
property of mean-preserving variance increases on far-right-tail
exceedance probabilities (not a defect in the distribution's design,
which Issue section 6.3 correctly prohibits "fixing" by lowering the
mean). The fix lives entirely in `objectives.py`: a config-driven discount
proportional to how much a ticker's own distribution was widened
(`reliability_sigma_multiplier`/`reliability_left_tail_extra`, two new
diagnostic-only distribution fields), tuned empirically against the real
universe rather than guessed. The most decision-relevant real measurement
-- `Spearman(severity, final ten_bagger rank)` across the full,
1,164-ticker universe -- moved from -0.0068 (no real relationship,
statistically indistinguishable from zero) to +0.230 (worse accounting
quality now correctly correlates with a worse rank placement). A
causally-cleaner isolated per-ticker check (same ticker, with vs without
the signal) shows the sign flips for 93% of the 712 affected tickers, with
an honestly-documented ~7% residual attributable to extreme-tail lognormal
sensitivity that a linear-in-severity discount cannot fully cancel at any
finite lambda -- reported as a real limitation, not tuned away by pushing
lambda to an extreme value for a rounder headline number.

Neither fix touches `scenario.py`, `quality.py`, or `tail_risk.py`; both
are provably mean-preserving (measured directly: `expected_moic`/
`expected_cagr` identical between a "clean" and a maximally "widened"
constructed pair, to `rel=1e-9`). Both are opt-in via new config fields
that default to a zero-effect no-op, which is why all 941 pre-existing
tests needed no modification -- 11 new regression tests were added
instead, and the full suite (952) passes.

This phase also corrected Phase 8's Definition-of-Done item 4 assessment:
it had been marked "not achieved" based on a memory note dated
2026-09-01, which predates this same multi-round effort's own Phase 0
work (commit `7f05790`, "model v5 Phase 0 data-quality corrections and
baseline record") that already fixed both the TAM trillion-scale bug and
the M&A/delisting `unknown`-treated-as-0% bug, with its own real-DB
re-verification recorded at the time. A fresh real-DB read this round
confirms no regression since (`delisting_events`: still 94/94 `unknown`,
correctly excluded from the acquisition-rate denominator;
`market_opportunity_estimate`: still 4 valid-magnitude rows). Item 4 is
corrected to **achieved**; a separate, different, still-open concern (low
TAM extraction *coverage*, not correctness) is recorded distinctly so it
is not mistaken for the same item.

None of the above changes Entry 1's central finding or this Decision
Record's conclusion: no realized-outcome backtest exists for either model,
so `PROMOTE_V5`/`KEEP_V4` remain unavailable per Issue #3 section 31.
`CONTINUE_SHADOW` stands.

---

*Future entries append below this line, in the same JSON + narrative shape,
oldest first.*

