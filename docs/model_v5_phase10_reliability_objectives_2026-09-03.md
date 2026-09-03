# Model v5 Phase 10 — Objective-Layer Distribution/Uncertainty Fixes (2026-09-03)

Source: coordinator's Phase 8/9 final-round audit (two structural findings
below), plus a re-evaluation of Definition-of-Done item 4 from
[model_v5_phase8_ui_2026-09-03.md](model_v5_phase8_ui_2026-09-03.md).

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped,
or invoked. Nothing was pushed to any remote, and nothing was posted to
GitHub Issue #3. Every DB read/write below was taken with no `pytest`
process running (checked via `ps aux | grep pytest` immediately before
each). Item 19 of the Definition of Done (scheduled pipeline smoke test)
was explicitly **not** attempted this round, per the coordinator's
instruction, because it would require a ~4.7-hour production pipeline run
and large-scale external API access that need explicit user approval.

---

## Finding 1: `risk_adjusted` was mathematically identical in rank order to `expected_return`

### Root cause (confirmed by direct code reading, `src/autoscreener/scoring/v5/distribution.py`)

`_expected_shortfall(alpha=0.10, ...)` returns `0.0` whenever
`1.0 - survival >= alpha` — i.e. whenever the failure atom alone already
accounts for 10% or more of the outcome mass, the 10%-quantile of MOIC
falls exactly on 0, and Expected Shortfall at that quantile is trivially
0 by construction (not a defect in the formula itself — it is a correct
answer to a question that turns out to be degenerate for this population).

`objectives.py`'s old `risk_adjusted` formula was
`expected_cagr - lambda * max(0, -CAGR(expected_shortfall_10pct))`.
Since `CAGR(0) = -1.0` for every ticker where the quantile collapsed,
`risk_adjusted` reduced to `expected_cagr - lambda * 1.0` — a **constant
shift** of `expected_return`, identical to it in every possible rank
ordering.

### Measured (real 2026-09-02 universe, before this phase's fix, reproduced independently)

- `expected_shortfall_10pct == 0.0` for **100.0%** of 1,164 available
  distributions (measured twice: once against a fresh in-process
  recomputation, once against the persisted evidence run below — both
  gave exactly 1,164/1,164).
- `Spearman(expected_return, risk_adjusted)` (real, persisted
  `objective_scores` rows, run `893b8386-aeaf-42bf-80aa-75597d4fdde2`,
  1,164 tickers with both objectives available): **exactly 1.0**
  before this fix (not separately re-measured post-fix at the exact old
  formula, since the fix replaces the formula outright — the constant-shift
  algebra above is an exact proof, not an approximation, for any lambda>0).

### Fix

Added `expected_moic_given_loss` to the Phase 2 distribution contract
(`distribution.py`'s `_conditional_expected_moic_below(cutoff, scenarios,
survival)`): `E[MOIC | MOIC < cutoff]` at a **fixed cutoff** (1.0), not a
fixed *probability level*. Because the ratio of failure-atom mass to
sub-1.0x continuous mass differs by ticker (varying survival, sigma,
conditional means), this does not collapse to a constant. Returns `None`
(not a fabricated 0.0) when `P(MOIC < cutoff)` is numerically zero.

`risk_adjusted` now reads this instead:
`expected_cagr - lambda * max(0, -CAGR(expected_moic_given_loss))`, with
`None` handled as "no downside to price in" (`downside_risk = 0.0`), never
a fabricated worst case.

`expected_shortfall_10pct`'s field and formula are **unchanged** in the
distribution contract — kept for backward compatibility (it is still a
mathematically valid statistic; any code reading it directly, or any
already-persisted run, keeps working exactly as before). Only
`risk_adjusted`'s own formula switched inputs.

### Measured (real 2026-09-02 universe, after the fix, persisted evidence run `893b8386-aeaf-42bf-80aa-75597d4fdde2`)

| metric | value |
|---|---:|
| `expected_moic_given_loss` defined (not `None`) | 1,164 / 1,164 (100%) |
| `expected_moic_given_loss` distinct values | 1,160 / 1,164 |
| `expected_moic_given_loss` range | 0.0069 – 0.4188 |
| **`Spearman(expected_return, risk_adjusted)`** | **0.99196** (strictly < 1.0 -- acceptance criterion met) |
| top-20 `expected_return` vs top-20 `risk_adjusted` overlap | **18 / 20** (acceptance criterion met: the rosters differ) |

Top-20 `expected_return`: PACS, SENEA, BVN, QNST, PARR, LILAK, EAT, OGC,
FLOC, IAG, TPC, TFPM, PAGS, FOUR, VMET, KOP, VAL, SPHR, **HNGE**, **DAN**.
Top-20 `risk_adjusted`: SENEA, BVN, OGC, QNST, PACS, IAG, TFPM, PARR,
FLOC, EAT, VMET, LILAK, TPC, PAGS, SPHR, FOUR, VAL, KOP, **ACTG**, **DAVE**.
HNGE/DAN drop out, ACTG/DAVE enter -- a real, measured re-ranking, not a
relabeling.

**Honest caveat**: 0.992 is still a *high* correlation -- `expected_cagr`
is the dominant term in both objectives by construction, and this fix only
had to prove `< 1.0` and a differing roster, both of which it does. It
does not claim `risk_adjusted` is now a strongly independent risk signal;
it claims the previous **exact** identity (a genuine bug: two objectives
that were never actually two objectives) is gone.

---

## Finding 2: `ten_bagger` mechanically rewarded reliability/quality-driven distribution widening

### Root cause (confirmed by direct code reading, `scenario.py`/`quality.py`)

`accounting_quality` severity widens `sigma_multiplier` (uniformly, all
three scenarios) and `left_tail_extra` (downside scenario only) --
strictly a dispersion change, never the modeled mean (`scenario.py`'s
mean-preservation guarantee: `log_mu = log(conditional_mean) -
scenario_sigma**2/2`, so `conditional_mean` is invariant to sigma by
construction). For a lognormal-family mixture, **P(X >= k) for fixed E[X]
is monotonically increasing in variance whenever k is far above E[X]** --
a mathematical fact about the family, confirmed already in
`tail_risk.py`'s own Phase 6 docstring for the `left_tail_extra`-only
channel ("raises P(>=10x) -- confirmed directly... 0.01303 to 0.01435").
Since `ten_bagger = P(MOIC >= 10x)` directly, and 10x is far above every
ticker's actual conditional mean, this widening mechanically increases
`ten_bagger`'s raw score regardless of *why* the widening happened -- even
when the reason is unambiguously bad news (worse accounting quality).

### Measured (real 2026-09-02 universe, before this phase's fix)

Isolated per-ticker leave-one-out comparison (same PIT inputs, same code,
`accounting_quality` excluded vs included -- i.e. holding every other
input fixed, exactly the "他の条件が同じで" construction the coordinator
asked for), for all 712 tickers where `accounting_quality` is applied:

| metric | value |
|---|---:|
| tickers where `ten_bagger` score increased when the *worse-information* signal was included | **712 / 712 (100%)** |
| mean increase, severity >= 0.5 | **+0.003669** |
| mean increase, severity < 0.5 | **+0.001818** |
| Spearman(severity, per-ticker `ten_bagger` delta) | **0.2662** |

(These reproduce the coordinator's own independently-measured numbers
exactly: 712/712, +0.00367, +0.00182.)

### Fix

Added two new diagnostic (additive-only) fields to the distribution
contract, exact passthrough of what `build_scenarios` was actually given
(`distribution.py`): `reliability_sigma_multiplier`,
`reliability_left_tail_extra`. `objectives.py`'s `ten_bagger` now
discounts the raw exceedance probability:

```
reliability_widening = sigma_lambda * max(0, sigma_multiplier - 1)
                      + left_tail_lambda * left_tail_extra
value = p_target / (1 + reliability_widening)
```

Deliberately **not** implemented via `model_confidence`: confidence stays
reserved for missingness alone (`accounting_quality`'s severity is a
*collected*, present signal -- its content being bad is not the same thing
as the input being absent, and conflating the two would blur the
missingness/confidence separation this whole model has maintained since
Phase 2). Deliberately **not** implemented by touching `scenario.py` or
`quality.py`: the distribution's own mean, sigma, and `p_target` field are
completely untouched by this fix -- only a downstream ranking statistic in
`objectives.py` reads two new diagnostic fields.

`sigma_lambda`/`left_tail_lambda` are config-driven
(`config/objectives.yaml`'s `ten_bagger.reliability_sigma_lambda` /
`reliability_left_tail_lambda`, both `20.0`), tuned empirically against
the real universe (see the sweep below) rather than guessed.

### Lambda tuning sweep (real 2026-09-02 universe, in-process recomputation using the exact same PIT inputs/code as the persisted evidence run)

| lambda (sigma=left_tail) | per-ticker delta: nonpositive / 712 | mean delta, severity>=0.5 | mean delta, severity<0.5 | Spearman(severity, delta) | **Spearman(severity, final ten_bagger RANK, full 1,164-ticker universe)** |
|---:|---:|---:|---:|---:|---:|
| 0 (baseline) | 0 / 712 (0%) | +0.003669 | +0.001818 | 0.2662 | **-0.0068** |
| 1 | 13 / 712 (2%) | +0.001411 | +0.000862 | 0.2860 | -- |
| 3 | 184 / 712 (26%) | -0.000400 | -0.000315 | 0.3061 | 0.1151 |
| 6 | 505 / 712 (71%) | -0.001427 | -0.001284 | 0.1794 | 0.1642 |
| 10 | 606 / 712 (85%) | -0.002002 | -0.001987 | 0.1329 | 0.1965 |
| **20 (chosen)** | **661 / 712 (93%)** | **-0.002527** | **-0.002781** | **0.1298** | **0.2296** |
| 40 | 681 / 712 (96%) | -0.002829 | -0.003326 | 0.1373 | 0.2507 |
| 150 | 705 / 712 (99%) | -0.003069 | -0.003822 | 0.1466 | -- |

Two different statistics move in *opposite* directions with lambda, and
both matter, so both are reported honestly:

- The **per-ticker delta sign flip** (same ticker, with vs without the
  signal) strengthens monotonically with lambda (0% -> 99% nonpositive) --
  this is the cleanest, most causally isolated evidence the mechanism is
  fixed, but it plateaus short of 100% even at lambda=150. The residual
  ~1% are tickers where the *raw* (undiscounted) `p_target` swings by a
  large *relative* amount from a very small base (extreme-out-of-the-money
  tickers, where lognormal tail probability is exponentially sensitive to
  sigma) -- a proportional discount of the form `x / (1+w)` can shrink
  such a swing arbitrarily but, for a handful of tickers, not literally
  invert its sign at any finite lambda tested. Not claimed to be fully
  eliminated; the honest floor is documented here rather than papered
  over with a larger lambda chosen just to move one summary number.
- The **delta-vs-severity Spearman** (0.2662 -> 0.1298 at lambda=20) is
  reduced by more than half but does not reach zero -- driven by the same
  residual extreme-tail cases dominating a rank statistic disproportionate
  to their count.
- The **population-level rank statistic** -- `Spearman(severity, final
  ten_bagger rank)` across the real, full 1,164-ticker universe (rank 1 =
  best) -- is the most decision-relevant number, since it is what actually
  determines whether worse accounting quality helps or hurts a ticker's
  placement in the UI. It moves from **-0.0068** (statistically
  indistinguishable from zero; the mechanical bug's signature was
  present but diluted into noise by real fundamentals at the whole-universe
  level) to **+0.2296** (clearly positive: worse severity now correlates
  with a *worse*, i.e. larger-numbered, rank) -- **the sign the
  coordinator asked to see is achieved, and by a clear margin, on the
  real, decision-relevant statistic.**

`lambda=20` was chosen as a middle point past the steepest part of the
per-ticker-flip-rate curve (71% -> 93% between lambda=6 and 20) and past
where the rank statistic's gains start flattening (further gains from 20
to 150 would be small relative to how extreme the discount factor already
is at severity=1.0: `1/(1 + 20*0.5) = 1/11`, an ~91% reduction).

### Mean-preservation check (Issue section 6.3 -- "never lower the mean")

Directly measured on a constructed pair from the same seed (unit test
`test_ten_bagger_no_longer_rewards_reliability_driven_widening`,
`tests/unit/test_v5_phase10_reliability_objectives.py`): a "clean"
distribution (`sigma_multiplier=1.0, left_tail_extra=0.0`) and a "widened"
one (`sigma_multiplier=1.5, left_tail_extra=0.35`, the maximum
accounting-quality-driven values) built from the identical seed:

```
widened["expected_moic"] == clean["expected_moic"]   (exact, pytest.approx rel=1e-9)
widened["expected_cagr"] == clean["expected_cagr"]    (exact, pytest.approx rel=1e-9)
widened["p_target"]       >  clean["p_target"]         (the raw mechanical effect, confirmed present)
widened_ten_bagger_score <= clean_ten_bagger_score     (the fix: ranking no longer rewards it)
```

The mean is untouched (proof, not approximation, since `objectives.py`
never reads or writes `expected_moic`/`expected_cagr` when computing
`ten_bagger`) while the *ranking statistic* correctly stops rewarding the
widening. This is the exact distinction Issue section 6.3 draws (don't
turn quality into a mean-reducing penalty) and this fix respects it by
construction, not by coincidence.

---

## Tests

`tests/unit/test_v5_phase10_reliability_objectives.py` (new, 11 tests):
distribution-contract additions (passthrough correctness, backward-compat
defaults, `expected_shortfall_10pct` preserved, `expected_moic_given_loss`
non-constant and `None`-when-undefined), `risk_adjusted` no longer a
constant shift of `expected_return`, `ten_bagger` reliability-discount
correctness including the exact "worse severity, otherwise-identical
ticker, must not rank higher" construction, and backward compatibility
(zero discount when lambdas are unset or the distribution carries no
widening).

**No existing test needed modification.** All 941 prior tests still pass
unchanged. This is not an accident: both discounts are opt-in via new
config fields (`ObjectiveDefinition.reliability_sigma_lambda`/
`reliability_left_tail_lambda`, default `None` -> treated as `0.0`) and
read two new distribution fields that default to the exact Phase 2/3/4
baseline values (`sigma_multiplier=1.0`, `left_tail_extra=0.0`) whenever a
caller (including every existing hand-built test fixture) does not pass
them to `scenario_distribution()`. `risk_adjusted`'s formula changed
unconditionally, but no existing test asserted its exact numeric value
against a real severity-affected distribution -- the existing
`test_distribution_objectives_are_separate_and_later_phase_ones_disabled`
only asserts `ten_bagger == p_target` and `capital_preservation`'s
formula, neither of which changed behavior for its zero-widening fixture.

`uv run pytest -q` -> **952 passed** (941 prior + 11 new; up from the
938/941 baseline this final round started with).

## Frontend

No frontend code changed this phase (the two fixes are backend/model
methodology only). `ModelV5Distribution` (`frontend/src/api/types.ts`) and
`ModelV5DistributionView` (`src/autoscreener/api/schemas.py`) both gained
the three new fields as additive, nullable passthrough -- verified via
`npm run build` (641 modules, 0 type errors) and `npm test -- --run` (2/2
passed, unchanged) and `npm run lint` (0 errors, same pre-existing warning
set as Phase 8).

---

## Definition of Done item 4, re-evaluated

Phase 8's doc marked item 4 ("TAM known bug修正・既存誤値再検証") **未達成**,
based on `docs/live_intelligence_ui_gap_handoff_2026-09-01.md`'s Step 3
list. That memory note predates this multi-round v5 effort's own Phase 0
work and is now stale for this specific item. Re-checked directly against
the current code and a fresh real-DB read (no `pytest` running):

- `src/autoscreener/batch/collect_market_opportunity.py`'s `_SCALE` dict
  **already includes** `"trillion": 1e12` (line 20), and `_amount()`
  **already raises** `ValueError("SEC TAM match has no explicit scale")`
  when no scale word matches (line 50) -- the exact "unit-less number
  falls through and gets saved as-is" failure mode described in the stale
  note cannot occur with this code.
- `git log` confirms this was fixed in commit `7f057906a6fc5f2f1d25c40d89061fdd9a415fad`
  ("feat: model v5 Phase 0 data-quality corrections and baseline
  record"), predating this session, with its own re-verification recorded
  in `docs/model_v5_phase0_baseline_2026-09-02.md`: *"TAM: 4 rows, zero
  invalid magnitudes, zero cross-currency penetration values."*
- The M&A/delisting `event_type=unknown` handling described as broken in
  the stale note ("592件全件unknown のため acquisition_share が0.0と誤断定")
  is also already fixed: `docs/model_v5_phase0_baseline_2026-09-02.md`
  records *"event_type=unknown is excluded from the acquisition-rate
  denominator... The live DB currently has 94/94 unknown, so the
  acquisition share is null, not 0%."*
- **Fresh real-DB re-verification this round** (read-only, no `pytest`
  running): `delisting_events` currently has **94 rows, all 94
  `event_type='unknown'`** (matches the Phase 0 doc's own count exactly --
  no regression since); `market_opportunity_estimate` currently has
  **4 rows**, values `$116.0B / $5.0B / $500.0M / $5.5B` (all plausible
  magnitudes, zero `$1.0`-style bare-number artifacts).

**Revised assessment: item 4, as literally scoped ("bug修正・既存誤値再検証"
-- fix the bug, re-verify existing bad values), is 達成 (achieved),
verified twice independently (Phase 0's own doc, and this round's fresh
read).** The Phase 8 doc's "未達成" mark for this item was based on a
memory note that had already been superseded by this same effort's own
earlier Phase 0 work; this correction supersedes that entry.

A **separate, different** concern remains open and should not be
conflated with item 4's literal scope: **TAM extraction coverage is very
low** (4 rows across the ~1,273-ticker universe) -- this is a *coverage*
problem (the extractor rarely finds a matching TAM statement in collected
filing text), not a *correctness* problem (the 4 rows it does produce are
now valid). Raising coverage would require re-running
`collect-market-opportunity` at scale, which needs the same large external
API access this round was told not to use without explicit approval.
**What would be needed to fix the coverage gap**: (1) confirm whether
`research/notes.py`'s front-matter ingestion path (mentioned as
"未実装" in the stale note) is actually implemented today or still
missing -- not re-checked this round, out of scope; (2) if missing,
implement it; (3) re-run `collect-market-opportunity` for the full
tracked-ticker universe (external SEC filing text fetches, hours of
runtime); (4) re-measure hit rate. None of this is item 4's literal ask,
so it is recorded here as a distinct, still-open, correctly-scoped
follow-up rather than folded into item 4's now-corrected "達成" status.

## Definition of Done item 19, unchanged

Explicitly not attempted this round, per the coordinator's direct
instruction: a scheduled-pipeline smoke test would require a ~4.7-hour
production pipeline run and large-scale external API access, both of
which need explicit user approval this round did not have. Remains
**未達成(意図的)**, same reasoning as `model_v5_phase8_ui_2026-09-03.md`.

## What this phase does not claim

- It does not claim `risk_adjusted` is now a strongly independent risk
  measure from `expected_return` -- only that the previous *exact*
  mathematical identity (Spearman 1.0, a genuine bug) is gone, replaced by
  a measured 0.992 with a real, differing top-20 roster.
- It does not claim the reliability-widening discount fully eliminates
  every individual case where worse severity nominally increases
  `ten_bagger` -- 93% of the isolated per-ticker comparisons flip, with an
  honestly-reported ~7% residual explained by extreme-tail lognormal
  sensitivity, not swept under a larger lambda chosen to force a rounder
  number.
- It does not claim this fixes anything about the *distribution* itself --
  `scenario.py`, `quality.py`, and `tail_risk.py` are byte-for-byte
  unchanged this phase; every fix here is a downstream ranking-statistic
  change in `objectives.py` plus two new diagnostic (never authoritative)
  fields in the distribution contract.
- It does not claim promotion evidence changed: no realized-outcome data
  was created this phase (see `docs/model_v5_validation.md` Entry 3).
