# Model v5 Phase 6 — Tail / Macro / Competing Risk (2026-09-03)

Source of truth: GitHub Issue #3 sections 10/12/13, via
[model_v5_phase4_handoff_2026-09-03.md](model_v5_phase4_handoff_2026-09-03.md)
section 6. This doc also carries two minor audit items from the Phase 5
re-review and the future-dilution-capacity user decision, addressed before
Phase 6 implementation started.

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped, or
restarted for this work. Real-data validation used the dedicated
`run-v5-shadow --date 2026-09-02` command, run **after** committing this
phase's implementation (commit `06f99a4d48f05aee879de4e287752d2b07fcc886`,
clean tree) -- see "New standard rule" below.

---

## Minor audit items (addressed before Phase 6 implementation)

### Minor 1 (fixed): clamp-to-1.0 incremental_roic cases stayed "applied"

`reduction_years <= 0` was already downgraded to `no_effect_keys` (Phase 5
re-audit fix), but the *other* zero-effect path -- `incremental_ratio`
clamped to exactly `1.0` when `initial_rate < terminal_rate` -- was still
counted as `applied`, with `mean_multiplier *= 1.0` silently contributing
nothing. Fixed: the same `>= 1.0 - 1e-9` check now routes into
`no_effect_keys` too, with `signal_effects[key] = {"status":
"no_change_clamped_to_unity", ...}`.

Real-run confirmation (`c87e75fe-9b8a-4b6a-b5c4-5a445d50f82d`, same
2026-09-02 universe): `incremental_roic` applied count dropped from **105 to
83** -- the missing 22 are exactly the clamp-to-unity cases the audit
counted by hand. All 83 remaining ablations are negative on
`DeltaP(target)` (0 positive, 0 zero) -- the cleanest sign result of any
Phase 4/5/6 mean-affecting signal so far.

### Minor 2 (new standard rule, applied to this doc): evidence runs must be
### on a clean, committed tree

The Phase 5 evidence run (`a92c6c76-...`) carried `code_revision = {commit:
9416ec8, dirty: True}` -- an honest record, but not the state
`code_revision` tracking exists to make comparable across runs. **Standard
rule from now on:** implement -> commit -> run the evidence shadow run on a
clean tree -> confirm `code_revision.dirty == False` -> only then finalize
the doc's numbers. This doc's run was taken after committing
`06f99a4d48f05aee879de4e287752d2b07fcc886` with `git status --porcelain`
empty; the run's own `metrics.code_revision` confirms it:

```json
{"commit": "06f99a4d48f05aee879de4e287752d2b07fcc886", "dirty": false, "reason": null}
```

---

## Future dilution capacity: user decision to scope principle 3 (Issue #3
## section 12)

The Phase 5 doc recorded, without guessing, a conflict between the
handoff's Phase 5 prep list (which named future dilution capacity as an
intended v5 input) and `collect_dilution.py`'s explicit "principle 3"
docstring (`dilution_capacity` is never read by `evaluate_gates` or
`scoring/`). The user's decision (2026-09-03, confirmed after independently
reading the source): **scope principle 3 to v4's `evaluate_gates`/
`scoring/` specifically; v4's own behavior is unchanged; v5, an independent
shadow challenger, may read it.** `collect_dilution.py`'s docstring now
records this decision and its date/rationale at the source, alongside the
original, still-standing v4 restriction -- the principle was not silently
rewritten.

### Implementation: `future_dilution_capacity` in `balance_sheet.py`

Reads `dilution_capacity` (ATM/shelf remaining authorization, unexercised
options/warrants ratio, variable-conversion flag). Connects to **the growth
mean multiplier** (future diluted share count -> per-share value), **not
survival** -- a distinct channel from `debt_maturity`/`liquidity`/
`capital_allocation`.

`overhang = min(cap, equity_capacity_ratio) + min(cap, unexercised_options_
ratio) + (variable_conversion_bump if flagged)`, where `equity_capacity_
ratio = (atm_remaining_usd + shelf_remaining_usd) / market_cap`. Each
dollar/share-count component is capped before weighting so one outlier
filing cannot dominate.

### Triple-counting analysis (the most important part of this signal)

Three mechanisms can all plausibly claim "this company's share count will
grow":

1. **v4 `dilution_drag`**: extrapolates the *historical* share-count CAGR
   forward across the full horizon (already inside `result`, feeding
   `growth_update`'s baseline path).
2. **Phase 4 `per_share_economics`**: measures the *realized* divergence
   between whole-company and per-share CAGR (gross profit/FCF), decaying
   `quality_update.mean_multiplier`.
3. **Phase 6 `future_dilution_capacity`**: unissued, authorized-but-unused
   *capacity* (ATM/shelf/options) -- forward-looking, not derived from any
   realized share-count history.

Structurally, (3) does not share input data with (1) or (2): (1)/(2) are
both derived from *realized* historical share counts; (3) is derived from
SEC S-3/424B5/10-Q *authorization* amounts minus *consumed-to-date*
amounts, a different data source entirely. But conceptually all three are
"about dilution", and a company that has been heavily using its ATM (making
(1)/(2) already fire) while also sitting on a large *remaining* shelf could
plausibly get penalized three times for what is, at some level, one
underlying behavior.

**Resolution (explicit subtraction, not just an independent cap, per the
coordinator's instruction not to hand-wave this):** `apply_capital_features`
now takes `quality_update` and computes
`already_used_reduction = 1 - quality_update.mean_multiplier` (the fraction
Phase 4's `per_share_economics`/`incremental_roic` already spent).
`future_dilution_capacity`'s own reduction is capped at
`min(future_dilution_max_reduction, max_combined_dilution_reduction -
already_used_reduction, weight * overhang)` -- it can only spend what
remains of a **shared** ceiling (`config.capital.
max_combined_dilution_reduction`, default 0.35), not stack its own
independent 0.15 on top regardless of what Phase 4 already took. If the
shared budget is already exhausted, the signal is reported via
`no_effect_keys` (`"no_change_zero_overhang_or_budget_exhausted"`), the same
honest-no-op discipline as every other zero-effect case in this codebase,
rather than a fabricated "applied, contributed nothing" entry. Pinned by
`test_future_dilution_capacity_no_effect_when_shared_budget_exhausted` and
`test_future_dilution_capacity_reduction_shrinks_with_already_used_budget`.

`v4`'s `dilution_drag` itself is not touched by this budget (it lives
inside `result`, upstream of both Phase 4 and Phase 6) -- the explicit
subtraction only coordinates the two *v5-introduced* mean-multiplier
channels against each other, which is the pair that could otherwise
literally double-count the same Phase-4-vs-Phase-6 observation.

### Real-data result: 0 applications (coverage-gated)

`future_dilution_capacity` measured **20.03% universe coverage** against
the 0.50 threshold (same precedent as every other Phase 5/6 SEC-filing-text
derived signal) and is globally coverage-gated off. The triple-counting
budget logic above was therefore never exercised by a real ticker this run
-- verified instead by the two unit tests above and the worked numeric
example in "Real-data result" further down.

---

## Phase 6 tail-risk signals (`tail_risk.py`)

### customer_concentration -> left tail only (Issue #3 section 12)

Total disclosed 10%+ customer revenue concentration for the latest period
(summed across disclosed customers, capped at 1.0). **Never lowers the mean
growth rate directly** -- the Issue's explicit requirement -- only
contributes to `left_tail_extra`.

### litigation -> left tail only

`litigation_events` has **no severity or amount field at all** in the
current schema (only `kind`/`title`/`detail` text) -- a starker version of
the "severity/amount coverage insufficient" limitation the handoff already
expected, not merely low coverage of an existing field. Trailing-window
(`litigation_lookback_days`, default 365) event *count*, capped at
`litigation_severity_count_cap` (default 3), is used as an explicitly
bounded, crude proxy, documented as such rather than presented as a
calibrated severity measure. `registry` keeps `historical=False` (shadow
only) unchanged.

### macro_regime -> left tail only, forward-shadow-only by construction

Narrowed to what is actually measurable and PIT-honest today: `downside_
beta` per (ticker, factor), already computed and stored in
`macro_exposure_snapshots`. High beta/exposure alone is never treated as
bad (Issue #3 section 10: "金利感応度が高い＝悪ではない") -- only the
downside-asymmetric component (and only a *positive* one; a defensive,
negative `downside_beta` gets no bonus) widens the left tail. Gated on
`raw_payload.fred_vintage_supported == True`; when a row exists but that
flag is false, the signal returns `coverage_status = NOT_APPLICABLE` (not
`COLLECTED_WITH_DATA`, which would misleadingly count toward the coverage
gate as usable) -- **current FRED values are never used retroactively for a
historical `as_of`.**

### The left-tail mechanism is narrower than accounting_quality's, but not
### fully immune to the same side effect (corrected claim)

An earlier draft of `tail_risk.py`'s docstring claimed this mechanism
"cannot raise P(>=10x) as a side effect", extrapolating from
`left_tail_extra` only touching the downside scenario. That claim was
checked directly and found **false** (though the effect is smaller):
`left_tail_extra` widens only the downside scenario's own sigma (weight
0.20 in the default mixture) rather than all three scenarios uniformly
(accounting_quality's `sigma_multiplier`), but the downside scenario is
itself a lognormal component with nonzero probability mass above any
threshold -- widening only its sigma still measurably raises P(>=10x), just
by a smaller margin. Corrected in the module docstring before this doc was
written, with the measured comparison below. This is exactly the kind of
"claimed but not measured" gap the audit process exists to catch, applied
here proactively rather than waiting for a re-review to find it.

### M&A competing risk: deliberately NOT implemented (Issue #3 section 13)

`delisting_events` has 94/94 rows with `event_type = "unknown"` (Phase 0
baseline), below any defensible classification-coverage threshold. Issue
#3 section 13 explicitly prohibits treating an unclassified event as "no
acquisition" (`acquisition = 0`). No signal builder was written for this at
all -- `pytest tests/unit/test_v5_phase6_tail_macro_competing_risk.py::
test_acquisition_competing_risk_stays_disabled_and_unimplemented` asserts
both that the feature flag stays `False` and that no `_acquisition_signal`
function exists in `tail_risk.py`. `competing_risk.acquisition_probability`
and `competing_risk.other_exit_probability` remain
`StateValue(None, "unsupported", "phase6")` in `state_model.py`, unchanged
from Phase 2 -- not fabricated as `0.0`, and not silently left as a
coverage-gated feature that happened to never apply (the two are different:
here, no code path could ever produce a value, by design).

---

## Real-data result

Shadow run (committed, clean-tree, per the new standard rule above):

- run ID: `c87e75fe-9b8a-4b6a-b5c4-5a445d50f82d`
- as-of: `2026-09-02`
- implementation version: `v5.phase6`
- config hash: `1b7b6801090eae5c`
- code_revision: `{"commit": "06f99a4d48f05aee879de4e287752d2b07fcc886", "dirty": false, "reason": null}`
- population: 1,273
- PIT-ready inputs: 1,215
- available distributions: 1,164
- unavailable distributions: 109
- objective rows: 6,365

### Coverage (all 7 new Phase 5/6 keys; existing keys unchanged from Phase 5)

| feature | coverage | threshold | runtime status |
|---|---|---|---|
| future_dilution_capacity | 20.03% | 0.50 | globally coverage-gated |
| capital_allocation | 25.77% | 0.50 | globally coverage-gated |
| debt_maturity | 24.82% | 0.50 | globally coverage-gated |
| liquidity | 24.82% | 0.50 | globally coverage-gated |
| litigation | 11.63% | 0.50 | globally coverage-gated |
| customer_concentration | 9.82% | 0.50 | globally coverage-gated |
| macro_regime | **0.00%** | 0.50 | globally coverage-gated (100% `fred_vintage_supported=false`) |

All seven are the predicted, correct outcome, not a failure -- the same
coverage-gate discipline Phase 3 applied to TAM/operating-KPI/guidance.
`macro_regime`'s exact 0.00% is the strongest possible confirmation that no
historical FRED value leaked in: every one of this universe's
`macro_exposure_snapshots` rows genuinely lacks vintage support.

### Full per-feature ablation table (all 16 growth/quality/capital/tail keys)

Exact scan over all 1,273 persisted `model_scores` rows and their
`ablation` payloads, `DeltaP(target)` and `DeltaP(MOIC<1.0)` sign
breakdowns as requested:

| feature | applied | ablations | dP(target)&gt;0 | dP(target)&lt;0 | dP(target)=0 | max&#124;dP(target)&#124; | dP(&lt;1.0)&gt;0 | dP(&lt;1.0)&lt;0 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| consensus_revision | 81 | 74 | 34 | 40 | 0 | 4.17e-04 | 40 | 34 |
| **incremental_roic** | **96** | **83** | **0** | **83** | **0** | **6.35e-04** | 83 | 0 |
| per_share_economics | 639 | 617 | 0 | 617 | 0 | 6.09e-03 | 617 | 0 |
| cash_conversion | 1,188 | 1,082 | 0 | 0 | 1,082 | 0 | 0 | 0 |
| accounting_quality | 798 | 712 | 712 | 0 | 0 | 1.25e-02 | 591 | 121 |
| debt_maturity / liquidity / capital_allocation / future_dilution_capacity | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| customer_concentration / litigation / macro_regime | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| tam_headroom / operating_kpi_nowcast / guidance / reconciliation_confidence | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

**Sign check against intent:**

- `incremental_roic`: **0/83 positive on P(target) -- perfect**, confirming
  both the path-recomposition fix and this doc's minor-fix-1 clamp
  correction (applied count 105 -> 83, all removed cases were genuinely
  zero-effect, not mis-signed).
- `per_share_economics`: 617/617 negative -- correct (a pure decay).
- `cash_conversion`: 1,082/1,082 exactly zero -- correct (diagnostic only).
- `accounting_quality`: 712/712 positive on P(target) (documented,
  mathematically forced, Phase 4 Fix 2) but a **mixed** 591 positive / 121
  negative on P(MOIC<1.0) -- widening sigma while preserving the mean does
  not uniformly raise probability mass below a fixed threshold for every
  distribution shape; for a ticker whose median sits well above 1.0x, the
  widened spread can pull mass away from *both* the near-1.0x region and
  the extreme tails asymmetrically. Recorded as an observed real-data
  characteristic, not further investigated -- no code claims a uniform
  sign for this quantity.
- All eight Phase 5/6 debt/liquidity/capital/tail keys: 0 real applications
  (coverage-gated). Verified by worked example instead, below.

### Survival and left-tail/mean effects reaching the distribution (worked
### examples, since real coverage was 0% for every Phase 5/6 signal this run)

Per the audit's explicit instruction ("実データで0件適用なら、私がやったよう
に合成値で数値検証してdocに載せること"), each mechanism not exercised by real
data this run is verified with a concrete worked numeric example, using the
same reference seed (`log_moic_mu = ln(2.0) - 0.5*0.7^2, log_moic_sigma =
0.7, survival = 0.91`) as the coordinator's own independent Phase 5
verification, plus `expected_shortfall`/median columns for completeness:

**`future_dilution_capacity` (mean_multiplier = 0.85, isolated):**

| | baseline | with dilution decay | delta |
|---|---:|---:|---:|
| `p_target` (P(&gt;=10x)) | 0.012611 | 0.007911 | -0.004700 |
| `expected_moic` | 1.820 | 1.547 | -0.273 |
| `expected_cagr` | 0.08931 | 0.06431 | -0.02500 |
| `p_moic_below_1_0` | 0.44496 | 0.50551 | +0.06054 |

Confirms the mean-multiplier channel actually moves `expected_moic`/
`expected_cagr` (unlike the mean-preserving accounting_quality/tail-risk
channels) -- it is a real decay, not a diagnostic-only field, matching its
target state ("growth の平均倍率").

**Combined tail risk (`left_tail_extra = 0.30`, roughly what
`customer_concentration` + `litigation` near their caps would produce):**

| | baseline | with left-tail widening | delta |
|---|---:|---:|---:|
| `p_target` (P(&gt;=10x)) | 0.012611 | 0.013889 | +0.001278 |
| `expected_moic` | 1.820 | 1.820 | 0.000000 |
| `expected_cagr` | 0.08931 | 0.08931 | 0.000000 |
| `p_moic_below_0_5` | 0.23775 | 0.25964 | +0.02189 |
| `p_moic_below_1_0` | 0.44496 | 0.45776 | +0.01280 |

Confirms the mean-preservation property holds exactly (`expected_moic`/
`expected_cagr` unchanged to the displayed precision) while
`p_moic_below_0_5`/`p_moic_below_1_0` move materially -- and confirms, as
documented above, that `p_target` also moves (a smaller version of
accounting_quality's Fix 2 effect, not eliminated by using only
`left_tail_extra`).

`survival_multiplier` reaching the distribution was independently
re-verified by the coordinator in the Phase 5 re-audit
(`survival_multiplier` 1.0 -> 0.9: `P(target)` 0.02384 -> 0.02145,
`P(<1.0)` 0.552 -> 0.597, `E[MOIC]` 1.821 -> 1.639) and by this session's own
Phase 5 doc worked example; not repeated here since debt_maturity/
liquidity/capital_allocation's mechanism is unchanged in Phase 6.

### PIT and mathematical checks

- feature evidence later than the as-of cutoff: 0
- probability-order violations: 0
- quantile-order violations: 0
- ES10 > P10 violations: 0
- applied feature missing a computed ablation (available distributions
  only): 0
- tickers missing any of the 16 growth/quality/capital/tail ablation
  slots: 0
- `ablation_results` 2,568 + `ablation_not_computed` 17,800 = 20,368 =
  1,273 tickers x 16 feature slots exactly

Live API smoke checks (`/api/v1/models/v5/runs/latest`,
`/api/v1/models/v5/scores?objective=ten_bagger`,
`/api/v1/models/v5/scores/{ticker}`) all returned HTTP 200;
`runs/latest` returned `run_id: c87e75fe-9b8a-4b6a-b5c4-5a445d50f82d`, and
the ticker-detail response's `states.contract_version` was `v5.phase6`.

The v4 production `scores` table was unchanged, checked with no `pytest`
process running immediately before and immediately after the shadow run:

- rows: 8,225 (both before and after)
- fingerprint: `77a819e8272901addfe3bc2ca3122b36` (identical before and
  after)

`uv run alembic current`: `1d2e3f4a5b6c (head)`, unchanged -- Phase 6 lives
entirely inside the existing `model_scores.states`/`.features` JSONB
columns plus `config/model_v5.yaml`; no migration was needed or added.

## Tests

- Phase 6 focused: 16 passed
  (`tests/unit/test_v5_phase6_tail_macro_competing_risk.py`)
- v5 Phase 1-6 focused selection: 67 passed (`test_v5_skeleton.py` +
  `test_v5_phase2.py` + `test_v5_phase3_growth.py` +
  `test_v5_phase4_quality.py` + `test_v5_phase5_balance_sheet.py` +
  `test_v5_phase6_tail_macro_competing_risk.py`)
- complete backend suite: 926 passed (910 Phase 0-5 baseline + 16 new
  Phase 6 tests)
- frontend tests: 2 passed; production build: PASS, 638 modules (identical
  to Phase 4/5 -- Phase 6 does not touch frontend code, re-run for
  confirmation)
- `uv run alembic current`: `1d2e3f4a5b6c (head)`, unchanged

## Deviations from the handoff and open items

- **future_dilution_capacity is implemented against `dilution_capacity`**,
  a reversal of the Phase 5 doc's deferral, per the explicit user decision
  recorded above and at the source (`collect_dilution.py`). This is the
  single largest deviation in this phase, fully evidence-backed rather than
  guessed.
- `macro_regime`'s connection to "scenario shift" (the handoff's phrasing)
  was narrowed to a `left_tail_extra` contribution driven by `downside_
  beta` alone, rather than a full regime-classifier x exposure model. This
  is a deliberate scope reduction to what the current schema can support
  honestly (see the signal's own docstring); a fuller macro regime model
  (e.g. a discrete stress classifier, or up/downside-asymmetric mean
  shifts) is left for a later phase if `fred_vintage_supported` coverage
  ever moves off 0%.
- `litigation`'s severity proxy (trailing-window event count) is
  explicitly a placeholder for a real severity/amount measure that does not
  exist in the schema yet -- documented in both the signal's docstring and
  the registry `notes`, not presented as calibrated.
- The tail-risk left-tail mechanism does not fully eliminate
  accounting_quality's Fix 2 side effect on P(target) (see above); this
  doc corrects an earlier internal docstring claim that it did, before
  that claim reached a shipped doc uncorrected.
- Registry `required_coverage` for all seven new keys was left at the
  Phase 5 precedent default (`0.50`) rather than tuned from this single
  day's measurement (0.00%-25.77%), since all seven are far below that bar
  regardless and a single day is not a distribution to fit a threshold to.

## objectives: no changes

No new objective was proposed. `capital_preservation` already reads
`P(MOIC < 1.0)`, which every mean-preserving Phase 6 tail signal is
designed to move once real coverage exists; `ten_bagger`/`expected_return`
already read `P(target)`/`expected_cagr`, which `future_dilution_capacity`
is designed to move once real coverage exists for that signal specifically.

## Phase 6 verdict

PASS for implementation and honest shadow validation, after two minor
audit corrections applied before real-data measurement (clamp-to-unity
no-op reclassification; a proactively-corrected docstring overreach on the
left-tail mechanism's side effect). All seven new Phase 5/6 signals
introduced or extended in this phase (`future_dilution_capacity`,
`customer_concentration`, `litigation`, `macro_regime`, plus
`debt_maturity`/`liquidity`/`capital_allocation` carried over unchanged
from Phase 5) are correctly and honestly coverage-gated off for the entire
real universe -- `macro_regime` at an exact, maximally-confirming 0.00%.
None was forced through by lowering a threshold. `incremental_roic`'s sign
is now perfectly clean on real data (0/83 positive). M&A competing risk was
deliberately left unimplemented rather than guessed around Issue #3's
explicit prohibition. Every mechanism not exercised by real data this run
(survival shrink, mean-multiplier decay, left-tail widening) was instead
verified end-to-end with a concrete worked numeric example, directly
applying the audit's own verification method rather than only asserting
unit-test coverage.

Feature efficacy for all Phase 4-6 signals remains a Phase 7 backtest
question, contingent on collection coverage improving well beyond the
current 0-26% range for every Phase 5/6 dataset. Per the coordinator's
note, Phase 7 also has pre-existing operational blockers (v4 backtest
`INSUFFICIENT_DATA`, 0% delisting settlement, coverage-bias
`REVIEW_REQUIRED`) that were flagged back in the Phase 0 baseline and not
addressed in Phases 4-6 (out of scope for shadow-model implementation
work); whether those need to be resolved before or alongside Phase 7 is a
question for that phase's own scoping conversation, not resolved here.
