# Model v5 Phase 5 — Capital Allocation / Balance Sheet (2026-09-03)

Source of truth: GitHub Issue #3 sections 7/8/12, via
[model_v5_phase4_handoff_2026-09-03.md](model_v5_phase4_handoff_2026-09-03.md)
sections 3 (implementation contract) and 6 (Phase 5 prep). This doc also
carries the three residual audit items from the Phase 4 re-review, addressed
before Phase 5 implementation started as the coordinator required.

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped, or
restarted for this work. Real-data validation used the dedicated
`run-v5-shadow --date 2026-09-02` command. The v4 `scores` fingerprint was
taken with no `pytest` process running, per the residual-3 finding below.

---

## Residual audit items (addressed before Phase 5 implementation)

### Residual 1 (minor, fixed): incremental_roic sign flip in 22/105 cases

Root cause confirmed by the audit: when `initial_rate < terminal_rate` (a
company whose current growth sits below its own terminal rate), accelerating
fade toward the *larger* terminal rate raises the revenue-multiple path
instead of shortening it -- the exact opposite of what a low-incremental-ROIC
penalty should do. Fixed in `quality.py`'s `apply_quality_features`:
`incremental_ratio = min(1.0, incremental_ratio)` clamps the recomposed-path
ratio so it can never exceed 1.0 regardless of which direction the path
moves. Pinned by
`test_incremental_roic_never_increases_mean_multiplier_when_initial_below_terminal`.

Real-run confirmation (`a92c6c76-a2ff-4a21-ac08-da6ad4a898aa`, full table
below): **0/105 positive `DeltaP(target)` rows for incremental_roic**, down
from 22/105 before the fix. Those 22 cases now land at exactly `DeltaP == 0`
(the clamp makes them a genuine no-op rather than a wrong-direction effect),
and the other 83 remain negative (correctly penalizing), matching the
coordinator's exact instruction.

### Residual 2 (important, fixed): `model_runs` did not record a code revision

The pre-fix (`d92ea8fc`) and post-fix (`e2d2c611`) Phase 4 runs shared an
identical `config_hash`/`implementation_version` while producing materially
different results, because `config_hash` fingerprints
config/registry content only, never the Python source. Fixed: every
`ModelRun` now carries `metrics.code_revision = {"commit": <git rev-parse
HEAD>, "dirty": <bool>, "reason": <str|null>}`, recorded at run *creation*
(not only on success, so it survives a run that later fails) via
`engine.py: _code_revision_info()`. No migration: stored in the existing
`model_runs.metrics` JSONB column. Failure mode: git unavailable, not a
repo, or a timeout all degrade to `commit: null, dirty: null, reason:
"git_unavailable_or_not_a_repo"` rather than failing the run. Existing
historical runs (including `d92ea8fc`/`e2d2c611`) were **not** backfilled --
the append-only history is not rewritten. This run's own record:

```json
"code_revision": {"commit": "9416ec8a789613a0b5ca8add522a9052adadeb39", "dirty": true, "reason": null}
```

(`dirty: true` because Phase 5 code was uncommitted at the moment this
shadow run executed, which is expected mid-implementation and is exactly
the situation this field exists to make visible.)

### Residual 3 (methodology): unit tests write to the same database as shadow runs

Confirmed by direct inspection, not guessed: `tests/conftest.py` has no
database-isolation fixture at all (only rate-limiter reset and an
outbound-network block). `db/session.py`'s `session_scope()` always binds to
`get_settings().database_url` -- the same `DATABASE_URL` from `.env` that
`run-v5-shadow` uses, via a process-global `sessionmaker` cached in
`_SessionFactory`. `tests/unit/test_api_routes.py` (and
`test_forward_validation.py`, `test_llm_batches.py`) construct real `Score`
rows against this shared database and delete them at teardown; 88 of 92
files under `tests/unit/` call `session_scope()` at all. During the prior
audit round, running `pytest` concurrently with `run-v5-shadow` made the v4
row count transiently read 8,226 instead of 8,225 mid-suite.

**Fix applied (doc-only, as instructed):** added an explicit note to
`docs/model_v5_phase4_handoff_2026-09-03.md` section 5: take the v4
fingerprint only when no `pytest` process is running, and never run a
shadow run and the test suite at the same time. All fingerprint checks in
this document were taken with no `pytest` process active.

**Investigation only (not implemented, per explicit instruction):** is a
small, safe test-database isolation feasible?

- *Technically*, yes for the specific race observed: `Settings` is a
  `pydantic-settings` `BaseSettings` that already reads `DATABASE_URL` from
  the process environment (overriding `.env`), so a `conftest.py` fixture
  could set a different `DATABASE_URL` and reset `db.session._engine` /
  `_SessionFactory` before the first `get_engine()` call. Since Postgres's
  default `READ COMMITTED` isolation makes any `session_scope()` commit
  immediately visible to a concurrently-running `run-v5-shadow` connection,
  wrapping each test's `session_scope()` in a real, uncommitted
  savepoint/rollback (a standard SQLAlchemy testing pattern -- open a
  connection + outer transaction once, bind the session to it, `ROLLBACK`
  at teardown instead of ever committing) would fully prevent this specific
  race, not just shrink its window, because a row that is never committed
  is never visible to another connection in the first place.
- *Practically*, this is a bigger undertaking than "add a fixture": the
  overwhelming majority of the 88 DB-touching test files are not written as
  isolated unit tests with their own fixtures -- they query whatever real
  tickers/rows already exist in the dev database (this repository's own new
  Phase 4/5 tests do the same thing, e.g. `session.query(Ticker).order_by(
  Ticker.id).first()`). A genuinely separate, empty test database would
  break most of those files immediately unless it were also seeded with a
  full copy of real collected data (defeating "small") or every file were
  rewritten with self-contained fixtures (a project-sized effort, not
  proportional to this one race). The savepoint/rollback approach avoids
  needing a second database, but still requires auditing whether any test
  relies on multiple genuinely separate connections/commits mid-test (which
  a single-connection-per-test savepoint pattern cannot support) -- a
  per-file compatibility pass this session has not done.
- **Recommendation for a follow-up, not attempted here:** the
  savepoint/rollback fixture is the more promising and lower-risk of the two
  options, but it needs its own scoped pass (confirm no test depends on
  cross-connection visibility mid-test, verify `TestClient`-driven API tests
  route through the same patched session factory, and decide whether
  `run-v5-shadow`/other CLI invocations should also default to a
  developer-selectable database via an env var such as `DATABASE_URL`
  already supports today with no code change, just a documented `.env.test`
  convention).

---

## Implemented state updates (Phase 5)

New module `src/autoscreener/scoring/v5/balance_sheet.py`, same shape as
`quality.py`: `CapitalSignal` / `CapitalFeatureSet` / `CapitalUpdate`,
`build_capital_feature_sets()`, `apply_capital_features()`.

### debt_maturity -> refinancing_survival -> survival_probability

Reuses `routes.py:3619`'s exact `due_12m` definition (`DebtInstrument.
principal` summed where `maturity_date <= as_of + 365 days`, no lower bound
so already-past-due principal counts too) against `cash_balance +
revolver_available` from the latest PIT-visible `LiquidityFacility` row.
`shortfall = max(0, due_12m/available - 1)` when covered by cash; a bounded
sentinel of `1.0` (not an unbounded ratio) when `available <= 0` and debt is
due -- deliberately avoiding the `float("inf")`-in-JSONB defect Phase 4's
reconciliation signal hit. A fully-covered maturity wall gets `shortfall =
0` (no bonus for being well covered, matching every earlier phase's
convention). `debt_instruments` genuinely never having been scanned (vs.
scanned-and-clean) is disambiguated via the `debt_profile` coverage ledger,
reusing growth.py's exact `_LEDGER_DATASET` fallback pattern.

### liquidity -> refinancing_survival -> survival_probability

Cash runway from the latest PIT-visible annual `FinancialPeriod.
free_cash_flow` (reused from `V5PitInput.financial_annual`, the same Phase 4
field -- no new PIT plumbing needed) only when FCF is negative; an
FCF-positive or unknown-runway company gets no bonus. `shortfall = max(0,
liquidity_runway_floor_months - runway_months)`.

### capital_allocation -> refinancing_survival -> survival_probability

A trailing-window (`capital_allocation_lookback_days`, default 365) sum of
committed cash return (`buyback` + `dividend` event amounts) net of raised
capital (`debt_raise` + `equity_raise` event amounts), relative to the same
cash balance used by `liquidity`. **Reads only already-announced events in a
bounded trailing window; it never extrapolates a historical buyback rate
forward** (Issue #3 section 7's explicit requirement) -- there is no
multi-year compounding anywhere in this signal. A net capital *raiser*
(`net_commitment <= 0`) gets no penalty; only a large net cash *return*
relative to the cash balance is treated as a near-term liquidity-stress
signal. Note: the extractor (`investment_intelligence_extract.py:
extract_capital_events`) currently produces `event_type` in
`{buyback, dividend, capex, acquisition, divestiture, debt_raise,
equity_raise}` -- **`repayment` (in the handoff's prep list) does not
currently exist as an extracted type**, a difference between the prep note
and the actual extractor recorded here rather than guessed around; it is
simply absent from both the outflow and inflow sets.

### Composition (avoiding double/triple counting)

All three multiply together into one `survival_multiplier` (each `<= 1.0`,
enforced both in `apply_capital_features` per-signal and again in
`scenario.py`'s `build_scenarios` as defense in depth):
`survival_multiplier = debt_component * liquidity_component *
capital_allocation_component`. None of the three ever touches
`mean_multiplier`, `sigma_multiplier`, or any per-share/dilution field --
this is the whole reason survival_probability (never touched before Phase
5) was the right place for these three signals: **zero structural overlap**
with v4's `capital.diluted_share_factor`/`dilution_drag` or Phase 4's
`per_share_economics`/`cash_conversion`, since those all operate on the
mean/economics side and these three operate exclusively on the failure-atom
side.

### Deferred: future dilution capacity (NOT implemented -- documented conflict)

The handoff's Phase 5 prep list names a fourth candidate: ATM/shelf/
unexercised-option overhang from `dilution_capacity`, feeding uncertainty
(not mean, to avoid the double/triple-counting risk with `dilution_drag`/
`per_share_economics`). This was **not implemented**. Evidence found instead
of guessed around: `collect_dilution.py`'s own module docstring states, as
an explicit numbered design principle:

> 原則3: このバッチが書く `dilution_capacity` は `evaluate_gates` にも
> `scoring/` にも一切読まれない。表示・チェックリストのみが読者。

("Principle 3: the `dilution_capacity` this batch writes is never read by
`evaluate_gates` or `scoring/`; only display and checklists read it.")
`src/autoscreener/scoring/v5/` is literally a subpackage of `scoring/`.
Wiring `dilution_capacity` into any v5 scoring path -- even an
uncertainty-only signal that never touches the mean -- would silently
override this explicit, pre-existing repository principle rather than
extend it. Per the standing instruction not to guess when the handoff and
the current code conflict, this is left as an open decision for a human
(and, if resolved, Issue #3) rather than resolved unilaterally in either
direction. No registry key was added for it.

---

## Coverage-bias control and missingness

Same discipline as Phase 3/4: each feature requires universe-wide coverage
before being trusted for any ticker, verified directly in
`test_low_coverage_runtime_gate_disables_feature_even_with_a_row`.
Missing/failed inputs never move `survival_probability`; confidence is
handled separately via the same `-0.03`/`-0.08`/bounded `[-0.20, 0.20]`
missingness contract as every earlier phase's `FeatureSet.confidence_delta`.

## Real-data result

Real-DB shadow run:

- run ID: `a92c6c76-a2ff-4a21-ac08-da6ad4a898aa`
- as-of: `2026-09-02`
- implementation version: `v5.phase5`
- config hash: `cd2431356caaf64a`
- population: 1,273
- PIT-ready inputs: 1,215
- available distributions: 1,164
- unavailable distributions: 109
- objective rows: 6,365

Observed universe coverage for the three new Phase 5 keys (registry
threshold left at `0.50`, matching Phase 3's TAM/guidance precedent for
similarly SEC-filing-text-derived, similarly narrow-collection-scope
datasets -- these measurements are well below that bar either way, so no
threshold tuning was needed to reach the correct gated-off outcome):

| feature | coverage | registry threshold | runtime status |
|---|---|---|---|
| capital_allocation | 25.77% | 0.50 | globally coverage-gated |
| liquidity | 24.82% | 0.50 | globally coverage-gated |
| debt_maturity | 24.82% | 0.50 | globally coverage-gated |

This is the predicted, correct outcome, not a failure: SEC filing-section
text collection for debt/liquidity/capital-allocation facts is concentrated
in a minority of tracked tickers, and coverage-gating them off for the
*entire* universe (rather than only for tickers lacking a row) is exactly
what prevents "happened to be scanned" from becoming a rank advantage --
the same discipline Phase 3 applied to TAM/operating-KPI/guidance.

### Full per-feature ablation table (all 12 growth/quality/capital keys)

Exact scan over all 1,273 persisted `model_scores` rows and their
`ablation` payloads, including the requested `DeltaP(target)` sign
breakdown per feature:

| feature | applied | ablations | DeltaP&gt;0 | DeltaP&lt;0 | DeltaP=0 | max&#124;DeltaP&#124; |
|---|---:|---:|---:|---:|---:|---:|
| tam_headroom | 0 | 0 | 0 | 0 | 0 | 0 |
| operating_kpi_nowcast | 0 | 0 | 0 | 0 | 0 | 0 |
| consensus_revision | 74 | 74 | 34 | 40 | 0 | 4.17e-04 |
| guidance | 0 | 0 | 0 | 0 | 0 | 0 |
| **incremental_roic** | **105** | **105** | **0** | **83** | **22** | **6.35e-04** |
| per_share_economics | 617 | 617 | 0 | 617 | 0 | 6.09e-03 |
| cash_conversion | 1,082 | 1,082 | 0 | 0 | 1,082 | 0 |
| accounting_quality | 712 | 712 | 712 | 0 | 0 | 1.25e-02 |
| reconciliation_confidence | 0 | 0 | 0 | 0 | 0 | 0 |
| debt_maturity | 0 | 0 | 0 | 0 | 0 | 0 |
| liquidity | 0 | 0 | 0 | 0 | 0 | 0 |
| capital_allocation | 0 | 0 | 0 | 0 | 0 | 0 |

**Sign check against intent, feature by feature:**

- `incremental_roic`: intended sign is negative-or-zero only (a penalty for
  low reinvestment quality, never a bonus). **0/105 positive -- correct**,
  confirming the residual-1 fix. The 22 zero-effect rows are the
  `initial_rate < terminal_rate` cases the clamp now correctly neutralizes
  instead of inverting.
- `per_share_economics`: intended sign is negative-or-zero only (a decay).
  **617/617 negative -- correct.**
- `accounting_quality`: intended effect is "widen uncertainty, never lower
  the mean" -- **not** "always negative on P(target)". 712/712 positive is
  the expected, previously-documented, mathematically-forced consequence of
  mean-preserving sigma widening on a far-right-tail threshold (10x MOIC);
  see the Phase 4 doc's Fix 2 for the full mechanism. Correct given the
  design constraint, not a new anomaly.
- `cash_conversion`: intended effect is zero on the distribution by design
  (diagnostic state only). **1,082/1,082 exactly zero -- correct.**
- `consensus_revision`: intended sign is bidirectional (revisions can be up
  or down). **34 positive / 40 negative -- consistent with real analyst
  revision direction**, unchanged from the Phase 3 baseline.
- `debt_maturity`/`liquidity`/`capital_allocation`: no real-universe
  ablations this run (coverage-gated). Their intended sign
  (negative-or-zero on `p_target`, positive-or-zero on `p_moic_below_1_0`)
  is verified directly by unit test and by a worked example below, since
  real data cannot exercise them yet.

### Survival multiplier reaching the distribution (worked example, not real
### universe data)

Because 0/1,273 real tickers had an applied capital signal this run, the
"survival change reaches `p_moic_below_1_0`/`expected_moic`" check the audit
requested cannot be shown against real data yet. It is verified two ways
instead:

1. `test_survival_multiplier_moves_the_distribution` (unit test): a 0.85
   `survival_multiplier` on a 0.94-survival seed produces
   `0.94 * 0.85 = 0.799` in every scenario's `survival_probability`, and
   `build_scenarios` raises `ValueError` for any `survival_multiplier >
   1.0` (defense-in-depth guard, matching the `sigma_multiplier`/
   `left_tail_extra` guards from Phase 4).
2. `test_shadow_run_persists_capital_ablation_without_touching_v4` (engine
   test, synthetic ticker with a forced `debt_maturity` shortfall of 0.5):
   asserts `impact["scenario_impact"]["p_moic_below_1_0"] > 0` and
   `impact["state_shift"]["survival_multiplier"] < 0` end-to-end through the
   real engine/scenario/distribution code path.

A concrete worked example (same forced `debt_maturity` shortfall of `0.5`,
`survival_probability = 0.91` seed, default Phase 5 config weights):

| | without debt_maturity | with debt_maturity (shortfall=0.5) | delta |
|---|---:|---:|---:|
| `survival_multiplier` | 1.0 | 0.875 | -0.125 |
| resulting survival | 0.910 | 0.79625 | -0.114 |
| `p_moic_below_1_0` | 0.39723 | 0.47258 | +0.07535 |
| `expected_moic` | 2.28631 | 2.00052 | -0.28579 |
| `p_target` (P(&gt;=10x)) | 0.02700 | 0.02362 | -0.00338 |

Survival stress correctly (a) raises `p_moic_below_1_0` (more failure
probability), (b) lowers `expected_moic`, and (c) lowers `p_target` -- the
opposite direction from accounting_quality's mean-preserving sigma effect,
and for the structurally correct reason: shrinking `survival_probability`
moves the failure-atom mass itself, not just spreading probability around a
fixed mean.

### PIT and mathematical checks

- feature evidence later than the as-of cutoff: 0
- probability-order violations: 0
- quantile-order violations: 0
- ES10 > P10 violations: 0
- applied feature missing a computed ablation (available distributions
  only): 0
- tickers missing any of the 12 growth+quality+capital ablation slots: 0
- `ablation_results` 2,590 + `ablation_not_computed` 12,686 = 15,276 =
  1,273 tickers x 12 feature slots exactly

Live API smoke checks (`/api/v1/models/v5/runs/latest`,
`/api/v1/models/v5/scores?objective=ten_bagger`,
`/api/v1/models/v5/scores/{ticker}`) all returned HTTP 200;
`runs/latest` returned `run_id: a92c6c76-a2ff-4a21-ac08-da6ad4a898aa`, and
the ticker-detail response's `states.contract_version` was `v5.phase5`.

The v4 production `scores` table was unchanged, checked with no `pytest`
process running immediately before and immediately after the shadow run:

- rows: 8,225 (both before and after)
- fingerprint: `77a819e8272901addfe3bc2ca3122b36` (identical before and
  after)

`uv run alembic current`: `1d2e3f4a5b6c (head)`, unchanged -- Phase 5 lives
entirely inside the existing `model_scores.states`/`.features` JSONB columns
plus `model_runs.metrics.code_revision` (also JSONB) and
`config/model_v5.yaml`; no migration was needed or added.

## Tests

- Phase 5 focused: 10 passed (`tests/unit/test_v5_phase5_balance_sheet.py`)
- Phase 4 focused (incl. the two residual-1 fix tests): 17 passed
  (`tests/unit/test_v5_phase4_quality.py`)
- v5 Phase 1-5 focused selection: 51 passed (`test_v5_skeleton.py` +
  `test_v5_phase2.py` + `test_v5_phase3_growth.py` +
  `test_v5_phase4_quality.py` + `test_v5_phase5_balance_sheet.py`)
- complete backend suite: 910 passed (899 prior baseline + 1 residual-1
  regression test + 10 new Phase 5 tests)
- frontend tests: 2 passed; production build: PASS, 638 modules (identical
  to Phase 4 -- Phase 5 does not touch frontend code, re-run for confirmation)
- `uv run alembic current`: `1d2e3f4a5b6c (head)`, unchanged

## Deviations from the handoff and open items

- **future_dilution_capacity was not implemented** (see "Deferred" above)
  because of the explicit conflict with `collect_dilution.py`'s "原則3". This
  is the single largest scope deviation from the handoff's Phase 5 prep
  list and needs a human/Issue decision before Phase 6 or later revisits it.
- The `capital_allocation` extractor does not produce a `repayment` event
  type (only `buyback`/`dividend`/`capex`/`acquisition`/`divestiture`/
  `debt_raise`/`equity_raise` exist in
  `investment_intelligence_extract.py: extract_capital_events`), though the
  handoff's prep list names `repayment` as an expected type. The
  `capital_allocation` signal's net-commitment calculation simply does not
  reference it; a future extractor change adding `repayment` would need to
  decide whether it counts toward inflow (debt reduction funded by existing
  cash, arguably neutral) or is tracked separately.
- Registry `required_coverage` for the three Phase 5 keys was left at the
  Phase-3-precedent default of `0.50` rather than tuned from this single
  day's ~25% measurement, since 0.50 already correctly gates all three off
  and a single day is not a distribution to fit a threshold to. Re-measure
  if collection coverage for these datasets changes meaningfully.
- `apply_capital_features` does not take a `growth_update`/`quality_update`
  parameter (unlike `apply_quality_features`, which needed `growth_update`
  for the Fix 1 correction) -- survival_probability has no analogous
  path-recomposition need, since it is a flat multiplicative shrink applied
  directly to the seed value, not a duration/path effect.
- Residual 3's savepoint/rollback test-isolation recommendation is
  documented but intentionally not implemented, per the explicit
  instruction to investigate only.

## objectives: no new objective added

`capital_preservation` (`1 - P(MOIC < 1.0)`) already reads exactly the
quantity `debt_maturity`/`liquidity`/`capital_allocation` are designed to
move (confirmed by the worked example above: the forced example raised
`p_moic_below_1_0` by +0.075, which would lower `capital_preservation` by
the same amount). No new Phase 5 objective was proposed or added; the
existing objective already responds to the new state once real coverage
exists to activate it.

## Phase 5 verdict

PASS for implementation, honest shadow validation, and all three residual
audit items. `debt_maturity`, `liquidity`, and `capital_allocation` are
correctly and honestly coverage-gated off for the entire real universe
(24.8-25.8% coverage against a 0.50 threshold) rather than silently
favoring the minority of tickers with SEC-filing-derived debt/liquidity
data -- the same discipline Phase 3 applied to TAM/operating-KPI/guidance
and Phase 4 applied to `reconciliation_confidence`. The survival-probability
mechanism itself is verified end-to-end (unit tests + a worked numeric
example) to actually reach `p_moic_below_1_0`/`expected_moic`/`p_target`,
not just a display field, learning directly from the Phase 4 audit's core
lesson (an "applied" flag that never reaches `build_scenarios` is
undetectable without exactly this kind of ablation-vs-distribution
cross-check). Two residual defects from the Phase 4 audit (incremental_roic
sign flip, missing code-revision tracking) are fixed and confirmed with
fresh real-data measurements. The third (shared test/shadow-run database) is
honestly reported as investigated-not-implemented with a concrete
recommendation, rather than silently worked around. Feature efficacy remains
a Phase 7 backtest question, contingent on collection coverage for these
three datasets improving well beyond the current ~25%.
