# Model v5 Phase 7 — Backtest / Champion-Challenger Infrastructure (2026-09-03)

Source of truth: GitHub Issue #3 sections 3.3/25/26/27/31, via
[model_v5_phase4_handoff_2026-09-03.md](model_v5_phase4_handoff_2026-09-03.md)
section 6.

**This phase does not conclude that v5 is better or worse than v4.** Issue
#3 section 31 explicitly prohibits comparing a challenger against a broken
baseline, and as measured below, neither model currently has a
realized-outcome-based historical backtest available -- comparing them on
return/loss metrics today would be comparing two absences of evidence, not
evidence. This phase's deliverables are the three the coordinator asked
for: comparison infrastructure, a measurement of what is missing, and a
concrete list of what must become true for a valid comparison. See
[model_v5_validation.md](model_v5_validation.md) for the resulting
machine-readable Decision Record (`CONTINUE_SHADOW`).

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped, or
restarted for this work. Real-data validation used
`run-forward-validation-v5` (new) and direct calls to
`backtest/v5_comparison.py`'s functions, all run **after** committing this
phase's implementation (commit `6e0bafe60c56a5fe0421d49a7df301cac61cc614`,
clean tree, `code_revision.dirty = false` confirmed on every run below).

---

## Measured first: is a v5 historical backtest even possible? (before any design)

Per the explicit instruction to measure before designing, these numbers
were gathered before writing any Phase 7 code:

### `universe_snapshots(included)` -- the table `build_v5_pit_inputs` requires an exact match on

| snapshot_date | included count |
|---|---:|
| 2026-08-23 | 768 |
| 2026-08-24 | 704 |
| 2026-08-25 | 1,260 |
| 2026-08-28 | 1,260 |
| 2026-08-29 | 1,260 |
| 2026-08-30 | 1,191 |
| 2026-08-31 | 1,192 |
| 2026-09-01 | 1,191 |
| 2026-09-02 | 1,273 |

**Exactly 9 distinct dates exist in the entire table** (`min(snapshot_date)
= 2026-08-23`, `max(snapshot_date) = 2026-09-02` -- confirmed by querying
`SELECT DISTINCT snapshot_date` with no `included` filter, so this is not
an artifact of the `included=True` filter; the table itself has no older
rows).

### PIT-ready overlap (raw_snapshots + price_snapshots) for each of those 9 dates

| snapshot_date | included | raw_snapshots ready | price_snapshots ready | both |
|---|---:|---:|---:|---:|
| 2026-08-23 | 768 | 768 | 768 | 768 |
| 2026-08-24 | 704 | 704 | 704 | 704 |
| 2026-08-25 | 1,260 | 1,260 | 1,260 | 1,260 |
| 2026-08-28 | 1,260 | 1,260 | 1,260 | 1,260 |
| 2026-08-29 | 1,260 | 1,260 | 1,260 | 1,260 |
| 2026-08-30 | 1,191 | 1,191 | 1,191 | 1,191 |
| 2026-08-31 | 1,192 | 1,192 | 1,192 | 1,192 |
| 2026-09-01 | 1,191 | 1,191 | 1,191 | 1,191 |
| 2026-09-02 | 1,273 | 1,273 | 1,273 | 1,273 |

**100% overlap at every date** -- unsurprising, since these are the most
recent ~9 trading days and the collection pipeline runs same-day.

### The actual binding constraint: `raw_snapshots.available_from` range

- `raw_snapshots.available_from`: **2026-08-23 .. 2026-09-03** -- the exact
  same ~9-12 day window as `universe_snapshots`.
- `price_snapshots.trade_date`: **1993-01-29 .. 2026-09-02** (12,269 days).

Price history is not the constraint -- **fundamentals point-in-time history
is**. `raw_snapshots` (the table carrying the financial statements v5's
growth/quality/capital signals, and v4's own `MoicInputs`, are built from)
only exists for the last ~9-12 days. This is not a `universe_snapshots`
table gap that could be worked around by relaxing v5's exact-date-match
requirement -- even a fully re-derived, v4-style point-in-time
reconstruction (see below) would have nothing to reconstruct from before
2026-08-23, because the underlying fundamentals snapshots themselves do not
exist further back.

### v4's own backtest evaluation dates, and why they overlap so little with the above

`backtest/runner.py:_evaluate_one_date` builds each date's gated population
independently from `raw_snapshots`/`price_snapshots` history (it does
**not** require a stored `universe_snapshots` row for the historical date --
it re-derives point-in-time gates directly, unlike v5's exact-match
requirement). But **it discards any ticker/date whose realized return
cannot yet be computed** (`_realized_return(...) is None: continue`,
`runner.py`) before ever recording an `Observation`. Combined with
`rebalance_dates()`'s own bound (`latest = last_price_date - horizon_days`;
zero dates are returned if that falls before `first_price_date`), and given
the coordinator's own independently-confirmed v4 backtest figures (5.31 /
1.21 effective evaluation days across the two audit rounds), **v4's own
effective backtest window is also a handful of days, not years** -- for the
identical root-cause reason: no realized 7-year (or even multi-year)
outcome exists yet for stocks priced from `raw_snapshots`-vintage
fundamentals, because those fundamentals only started being preserved
point-in-time ~9-12 days ago.

**Conclusion, stated plainly: neither v4 nor v5 has a realized-outcome
historical backtest available today.** This is a shared, root-cause
data-maturity problem (the whole system's point-in-time fundamentals
history is ~9-12 days old), not a v5-specific implementation gap. Comparing
"v5 backtest results" to "v4 backtest results" would be comparing two
populations of essentially zero valid observations against a 7-year target
horizon (2,557 days) -- exactly the "broken baseline" comparison Issue #3
section 31 prohibits, on both sides at once.

---

## What Phase 7 delivers instead

### 1. Comparison infrastructure (`src/autoscreener/backtest/v5_comparison.py`)

- **`historical_feature_flags()` / `run_v5_historical()`** (Issue #3
  section 25): force-disables every registry feature with
  `historical_backtest_supported=False` (`litigation`, `macro_regime`,
  `acquisition_competing_risk`) regardless of `config/model_v5.yaml`, and
  records the forced-off set on the run. The harness's enforcement wins
  over config -- verified directly: real config has `litigation: true` and
  `macro_regime: true`, and `historical_feature_flags()` returns
  `forced_off = ('litigation', 'macro_regime')` (`acquisition_
  competing_risk` was already `false` by config, so it is correctly
  reported as pre-disabled, not force-disabled).
- **`compare_v4_v5_same_day()`**: read-only, same-day cross-sectional
  comparison between v4's live `scores` and a v5 run on the same `as_of`.
  Explicitly not a backtest -- no realized return is read or required, and
  the output dataclass carries `not_a_backtest=True`/
  `decision_input_only=True` plus warnings stating exactly why return
  metrics are not computable, so this cannot be mistaken for backtest
  evidence by a later reader. Population, feature coverage,
  `code_revision`, config hashes, rank correlation (reusing the existing
  `backtest/metrics.py: spearman()`, not a re-derived implementation),
  sector concentration, and size distribution are all included.
- **`date_block_bootstrap_ci()`**: date-block (whole-date, not per-ticker)
  bootstrap confidence interval, refusing to compute below
  `MIN_BOOTSTRAP_DATES = 30` distinct evaluation dates rather than
  reporting a fabricated-precision interval from a handful of blocks --
  the real current count (9) is well below this floor. Regime
  stratification and bias auditing (`backtest/stratify.py`'s existing
  machinery) were **not** attempted at n=9: that machinery operates on
  `Observation` objects that require a `realized_return`, which v5 cannot
  produce at all today (see above) -- there is no meaningful way to run it
  yet, not merely an inconvenient one.

### 2. PIT enforcement is real, testable infrastructure today even though it
### changes nothing yet

At current real coverage (`litigation` 11.63%, `macro_regime` 0.00%, both
measured in the Phase 6 doc), the ordinary coverage gate already disables
both features universe-wide, so forcing them off in historical mode
produces the same population either way *today*. The enforcement still
matters and is still tested (`test_historical_feature_flags_force_disables_
pit_unsupported_features`, `test_historical_feature_flags_never_force_
enables_anything`) because coverage will not stay this low forever, and the
harness must not depend on coverage happening to already be low for
correctness.

### 3. Forward validation (Issue #3 section 27)

New `model_v5_forward_returns` table (migration `2c4e6f8a1b3d`, verified
upgrade -> downgrade -> upgrade against the real database) and
`run_forward_validation_v5()` in `scoring/forward_validation.py`, reusing
that module's `_entry_price`/`_exit_price`/`_is_delisted`/
`_settle_delisted` logic **unchanged** -- only the source (`model_scores`,
keyed by `run_id`) and target table differ from v4's `run_forward_
validation()`. Exposed as `run-forward-validation-v5` (a standalone CLI
command, the same pattern `run-v5-shadow` used before being wired into the
daily pipeline). The `forward_validation_v5` pipeline stage number (`26`)
is reserved at the end of `PIPELINE_STAGE_SEQUENCE`, per the explicit
instruction not to renumber existing stages, but is **deliberately not
spliced into `daily_pipeline.py`'s actual execution list** this phase -- a
fresh, production-unvalidated code path should not be added to the live
09:00 JST scheduled pipeline as a side effect of an infrastructure
deliverable (see Deviations).

---

## Real-data result

### Historical-mode runs across all 9 available evaluation dates

`run_v5_historical()` (PIT enforcement applied) was run for real against
every one of the 9 `universe_snapshots(included)` dates, on the clean,
committed tree (commit `6e0bafe6...`, `code_revision.dirty = false`
confirmed on every run):

| as_of | run_id | status | population |
|---|---|---|---:|
| 2026-08-23 | `405ebdd4-0865-42f8-b7f7-4408625e2ae9` | succeeded | 768 |
| 2026-08-24 | `58eab09c-3d02-472d-919e-a2080f17ce08` | succeeded | 704 |
| 2026-08-25 | `5eec735c-26da-4c0e-aacb-2db683181068` | succeeded | 1,260 |
| 2026-08-28 | `84d4907b-ae84-415c-9a79-7f58d49178d5` | succeeded | 1,260 |
| 2026-08-29 | `0771972d-d963-4004-a23f-1de6601ab816` | succeeded | 1,260 |
| 2026-08-30 | `baf5cf78-aeeb-43d2-a3f6-1a01cbfc4817` | succeeded | 1,191 |
| 2026-08-31 | `96511662-8ee2-4636-9205-367553529abd` | succeeded | 1,192 |
| 2026-09-01 | `c3f1fa70-9ce4-415c-af13-0f227916f73b` | succeeded | 1,191 |
| 2026-09-02 | `5585e157-b585-49aa-966a-d9dc7e6999d6` | succeeded | 1,273 |

All 9 succeeded; population matches each date's `universe_snapshots
(included)` count exactly. **The comparison harness works across every
currently-available evaluation date, not just one.** Total population
across all 9 runs: 10,099 ticker-date pairs.

### Cross-date ablation summary (all 9 historical-mode runs combined)

Exact aggregation of `applied`/`computed` ablation counts and mean
`|DeltaP(target)|` per feature, across all 10,099 ticker-date pairs above:

| feature | applied (sum across 9 dates) | mean&#124;dP(target)&#124; |
|---|---:|---:|
| cash_conversion | 8,522 | 0 (diagnostic only, by design) |
| accounting_quality | 5,670 | 1.988e-03 |
| per_share_economics | 4,920 | 1.605e-04 |
| incremental_roic | 666 | 6.414e-05 |
| consensus_revision | 74 | 2.113e-05 |
| tam_headroom / operating_kpi_nowcast / guidance / reconciliation_confidence | 0 | 0 |
| debt_maturity / liquidity / capital_allocation / future_dilution_capacity | 0 | 0 |
| customer_concentration / litigation / macro_regime | 0 | 0 |

Consistent with every single-date measurement in the Phase 4/5/6 docs:
only the five financial-statement-derived Phase 4 signals apply on real
data across **every** available date, not just 2026-09-02 in isolation.
`litigation`/`macro_regime` show 0 applied here both because of ordinary
coverage gating and because historical mode force-disables them --this run
cannot distinguish which cause dominates, since both would produce the
same zero, and that ambiguity is recorded rather than resolved by guessing.

### Same-day comparison (`compare_v4_v5_same_day`, 2026-09-02, real v4 `scores` + real v5 run)

```json
{
  "evaluation_date": "2026-09-02",
  "v4_config_hash": "eb5be5480866aab3",
  "v5_run_id": "5585e157-b585-49aa-966a-d9dc7e6999d6",
  "v5_config_hash": "e685b39f8db78fcf",
  "code_revision": {"dirty": false, "commit": "6e0bafe60c56a5fe0421d49a7df301cac61cc614", "reason": null},
  "v4_population": 1165,
  "v5_population": 1273,
  "overlap_population": 1164,
  "rank_correlation_spearman": 0.9069484517268488,
  "size_distribution_v4": {"n": 1165, "median": 2138040326.445, "p10": 342661626.25, "p90": 7658398882.395},
  "size_distribution_v5": {"n": 1165, "median": 2138040326.445, "p10": 342661626.25, "p90": 7658398882.395},
  "not_a_backtest": true,
  "decision_input_only": true
}
```

(Full sector breakdown and `v5_feature_coverage` in the raw output; both
models' size distributions are identical because the overlap population's
`MoicInputs` are read from the same `Score.inputs` blob for both sides --
this is expected, not a bug, since v5 does not yet independently move
market cap.)

**Spearman rank correlation of 0.907** between v4's `probability` and v5's
`ten_bagger` objective score, on 1,164 overlapping tickers. This is a real,
informative, non-promotion-relevant measurement: it shows v5's ranking has
not diverged wildly from v4's on the day both were measured, which is the
expected and correct outcome given the current coverage-gate reality (the
Phase 4-6 signals that could meaningfully differentiate v5 from its v4-seeded
structural core apply to only a fraction of the universe -- see the Phase 4/5/6
docs' coverage tables). It says nothing about which model's ranking is
*better*, and this doc does not claim it does.

### Forward validation: 0 matured, as measured (not assumed)

```
run_forward_validation_v5() -> {'computed': 0, 'settled_delisted': 0, 'not_matured': 0, 'missing_price': 0}
```

Zero, including zero `not_matured` -- because the function's own maturity
pre-filter (`ModelRun.as_of <= as_of_date - _MIN_HORIZON_DAYS`, the same
30-day floor v4's `run_forward_validation()` uses for its shortest 1M
horizon) excludes every one of this session's real runs (all `as_of` in
2026-08-23..2026-09-02) before the per-ticker loop even starts, since none
is yet 30 days old relative to today. This is the expected, correct
behavior of a function that has nothing to settle yet -- confirmed by
running it for real, not assumed from the table being new. Verified
never to touch v4's `forward_returns`/`scores` tables (also confirmed by
the unit test `test_forward_validation_v5_never_touches_v4_tables`).

### PIT and mathematical checks

Not applicable in the Phase 4-6 sense (no new distribution mechanism was
added this phase) -- the 9 historical-mode runs above use the identical
`run_v5_shadow` code path already covered by the Phase 4-6 PIT/probability-
order/quantile-order/ES10/ablation-completeness checks, just invoked 9
times with a config override. No new violations are expected or were
found; not re-verified per-run in this doc to avoid restating Phase 4-6
evidence.

### v4 unchanged

Checked with no `pytest` process running, immediately before the 9
historical runs and immediately after the same-day comparison and forward-
validation calls:

- rows: 8,225 (both before and after)
- fingerprint: `77a819e8272901addfe3bc2ca3122b36` (identical before and
  after)

`uv run alembic current`: `2c4e6f8a1b3d (head)` -- the one new table this
phase added (`model_v5_forward_returns`), migration upgrade/downgrade/
upgrade verified against the real database (table dropped on downgrade,
restored with correct columns on re-upgrade).

## Tests

- Phase 7 focused: 10 passed
  (`tests/unit/test_v5_phase7_backtest_infrastructure.py`)
- complete backend suite: 936 passed (926 Phase 0-6 baseline + 10 new
  Phase 7 tests)
- frontend tests: 2 passed (Phase 7 does not touch frontend code, re-run
  for confirmation; production build not re-run this round since nothing
  frontend-adjacent changed)
- `uv run alembic current`: `2c4e6f8a1b3d (head)`

---

## What must become true for a valid v4-vs-v5 backtest comparison

Concrete, not aspirational -- each item below is directly implied by a
measurement above, not a generic wishlist:

1. **`raw_snapshots` point-in-time history must extend meaningfully beyond
   ~9-12 days.** This is the single binding constraint for both models.
   Since the target horizon is 7 years, a *statistically minimal* backtest
   (even just enough for `date_block_bootstrap_ci`'s 30-date floor at a
   *much shorter* interim horizon, e.g. 1M or 3M, not the full 7Y) needs
   raw_snapshots history stretching back at least 30 rebalance intervals
   plus that interim horizon -- for the default 91-day (3M)
   `DEFAULT_REBALANCE_INTERVAL_DAYS` cadence, roughly 30 x 91 + 91 = ~2,821
   days (~7.7 years) of `raw_snapshots` history, accumulating from today
   forward, before even an *interim*-horizon date-block bootstrap clears
   Phase 7's own `MIN_BOOTSTRAP_DATES` floor. The full 7-year target-horizon
   backtest needs 7 years of accumulated history beyond that.
2. **`universe_snapshots` must be written daily going forward without
   gaps**, so `build_v5_pit_inputs`'s exact-date-match requirement has a
   row for every date raw_snapshots history eventually supports. (This is
   an operational continuity requirement, not a code change -- the
   9-for-9 date coverage measured above shows the current collection
   cadence already does this correctly when it runs.)
3. **v4's own operational blockers (flagged by the coordinator, not
   resolved in this phase)** need their own resolution track, independent
   of v5:
   - v4 backtest KPIs are `INSUFFICIENT_DATA` for all KPIs (effective
     evaluation days 5.31 / 1.21) -- the same root cause as above.
   - Delisting settlement is 0% in v4's *backtest* population specifically
     (`backtest/runner.py`'s `_realized_return`) despite the daily-pipeline
     `forward_returns` mechanism having its own settlement logic
     (`_settle_delisted`) that this phase's `run_forward_validation_v5()`
     reuses -- these are two different code paths with different histories
     and should not be conflated when this gets resolved.
   - Coverage-bias audit remains `REVIEW_REQUIRED` (Spearman 0.825 between
     v4 probability and Live-dataset-with-data, per the Phase 0 baseline) --
     a live-collection-scope bias, independent of the backtest-window
     problem above, that would still need resolving even once (1) and (2)
     are satisfied.
4. **A decision on whether interim-horizon (1M/3M/6M/1Y) forward validation
   is an acceptable substitute for a full 7-year backtest during the
   multi-year accumulation period in (1).** `run_forward_validation_v5()`
   already computes all of `HORIZONS` (not just 7Y), so this data will
   exist as soon as (1)/(2) progress even partially -- but whether a
   `PROMOTE_V5`/`KEEP_V4` decision may ever be based on an interim horizon
   rather than the full 7Y target is a policy question for Phase 9, not
   something this phase decides.

None of the above is within Phase 7's scope to fix -- Phase 7's job was to
build the harness and measure the gap honestly, both done. Whether (3)
needs to be resolved before or alongside further v5 phases is, per the
coordinator's own framing, a question for a separate scoping conversation.

## Deviations from the handoff and open items

- **`forward_validation_v5` pipeline stage is reserved (`26`) but not
  wired into `daily_pipeline.py`'s execution list.** The handoff's
  instruction was conditional ("if you add it to the pipeline"); given the
  standing prohibition on touching the live 09:00 JST scheduled batch, this
  session judged that splicing a fresh, only-unit-tested code path into
  that pipeline's actual execution -- which would affect the *next*
  scheduled run without this session having validated it end-to-end in
  that context -- carries more operational risk than the deliverable
  requires. The function and CLI command are real, real-data-tested, and
  ready to be wired in as a deliberate, separately-reviewed follow-up.
- **Regime stratification and bias auditing (`backtest/stratify.py`) were
  not attempted**, not even at reduced rigor -- that machinery requires
  `Observation` objects with a `realized_return`, which literally does not
  exist for v5 yet (see above). This is a structural blocker, not a
  time/effort tradeoff, and building a fake substitute would violate the
  standing "no fabricated-precision statistics" rule.
- **`date_block_bootstrap_ci()`'s `MIN_BOOTSTRAP_DATES = 30` is a judgment
  call**, not a value derived from the handoff or Issue #3 text (which
  does not specify a minimum). Chosen to match the same order of magnitude
  as v4's own `KpiAcceptanceConfig.min_effective_dates` (default 6, though
  that gates a different, coarser KPI-verdict decision) times a
  block-bootstrap-appropriate multiplier; recorded here as a deviation
  since it is not sourced from the primary documents.
- **A dedicated, permanent `aggregate_ablation_across_dates()` library
  function was not added** to `v5_comparison.py`; the cross-date ablation
  summary above was computed via a one-off evidence script querying the 9
  real runs' persisted `ModelScore.features["ablation"]` directly. Given
  the population is only 9 dates today, a permanent aggregation function
  would currently only ever be exercised at that same tiny scale; adding
  one is deferred to whenever historical depth (see "What must become
  true" above) makes it load-bearing rather than decorative.
- **`compare_v4_v5_same_day`'s sector/size distribution for v5 currently
  reads v4's `Score.inputs`** for the overlapping tickers (v5's own
  `ModelScore.states` does not carry `market_cap`/`sector` -- they are v4
  `MoicInputs` concepts, not part of the Phase 2 state contract). This
  means the "v5" distribution shown is really "the v4-known distribution
  of v5's population," not an independently-computed v5 view. Correct for
  today's purpose (both models score the same underlying companies) but
  worth flagging before this function's output is used for anything more
  load-bearing.

## objectives / promotion: explicitly out of scope

No `PROMOTE_V5`/`KEEP_V4` judgment is made in this document. See
[model_v5_validation.md](model_v5_validation.md) for the Decision Record,
which records `CONTINUE_SHADOW` with the reasons measured above.

## Phase 7 verdict

PASS for infrastructure delivery and honest measurement, explicitly NOT a
promotion verdict. The central finding -- neither v4 nor v5 has a
realized-outcome historical backtest available today, for the identical
root-cause reason (raw_snapshots point-in-time history spans only ~9-12
days against a 7-year target horizon) -- was measured before any Phase 7
code was written, exactly as instructed, and it materially changed this
phase's scope from "run a backtest comparison" to "build the comparison
infrastructure and measure why a backtest comparison cannot run yet."
The comparison harness, PIT enforcement, and forward-validation
infrastructure are all real, tested against the live database (9 real
historical-mode runs across every available date, one real same-day
comparison, one real forward-validation call), and ready to produce
meaningful results the moment enough point-in-time history accumulates --
which, per the measurement above, is a multi-year proposition, not a
configuration change.
