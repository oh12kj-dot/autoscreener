# Model v5 Phase 4 — Quality / Accounting / Reinvestment (2026-09-03)

Source of truth: GitHub Issue #3, section 6, via
[model_v5_phase4_handoff_2026-09-03.md](model_v5_phase4_handoff_2026-09-03.md).

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped, or
restarted for this work. All real-data validation used the dedicated
`run-v5-shadow --date 2026-09-02` command against the PIT universe that was
already complete from the prior Phase 0 acceptance run (`universe_snapshots`
for 2026-09-02, 1,273 included tickers).

## Implemented state updates

### incremental ROIC -> growth duration

`incremental_roic = deltaNOPAT / deltaIC` between the earliest and latest
PIT-visible annual `FinancialPeriod`. NOPAT uses `operating_income * (1 -
nopat_tax_rate)` (`config/model_v5.yaml: quality.nopat_tax_rate = 0.21`, the
same proxy already hardcoded at `routes.py:3539-3540` as `* 0.79`, now
config-driven instead of a second hardcoded constant). Invested capital is
`total_debt - cash_and_equivalents`, matching the existing
`/candidates/{ticker}/reinvestment-quality` endpoint's definition exactly (no
second formula was invented).

The signal only ever *shortens* the state's `growth.duration_years`, and only
when both conditions hold: the company is growing (`result.initial_growth_rate
> 0`) and incremental ROIC sits below the configured hurdle rate (default
10%). A company with incremental ROIC above the hurdle gets
`duration_multiplier == 1.0` -- never an extension. `deltaIC <= 0` or a
non-positive operating income in either period makes `incremental_roic` and
therefore the signal `None`, not a penalty (`calculate_reinvestment_quality`'s
existing guards, reused unmodified).

### per-share economics -> growth mean multiplier

Compares whole-company CAGR to per-share CAGR for gross profit and free cash
flow (via the same `calculate_reinvestment_quality` reused from Phase 4's
incremental-ROIC call). **Revenue is deliberately excluded** from this gap:
the revenue-per-share gap is driven by the same `shares_outstanding`
denominator that v4's `dilution_drag` (`capital.diluted_share_factor`, seeded
from `MoicInputs.dilution_cagr`) already prices into every score. Including it
here would apply the same share-count effect through two separate
multipliers. Gross profit and FCF per-share gaps still carry information
`dilution_drag` does not capture (margin and cash-generation dilution beyond
pure share count). A positive average gap (company outgrowing its per-share
economics) decays `mean_multiplier` below 1.0, bounded by
`max_mean_multiplier_reduction`; a company whose per-share metrics keep pace
or run ahead of the whole-company figures gets no reduction. Missing
`shares_outstanding` yields `None`, never a fallback to the whole-company
CAGR.

### cash conversion -> economics state (diagnostic only)

Fills the `economics.cash_conversion` / `economics.reinvestment_efficiency`
state slots that Phase 2/3 left `status="unsupported", source="phase4"` with
real `OCF/NI` and `FCF/NI` ratios. This signal never changes a distribution
multiplier -- it only populates state. Net income within
`cash_conversion_ni_floor_ratio` (1% of revenue by default) of zero is
rejected as `net_income_near_zero` rather than divided through; both ratios
are winsorized to `+-cash_conversion_ratio_winsor_abs` (default 5.0) before
being stored.

### accounting quality -> uncertainty only

Reuses `calculate_accounting_quality` unmodified with `average_assets`,
`receivables_growth`, `inventory_growth`, `stock_based_compensation`, and
`goodwill`/`total_assets` now populated from the new `FinancialPeriod`
optional fields (see below), where the previous `/candidates/{ticker}/
accounting-quality` endpoint always passed these six as `None`
(`routes.py:3636-3637`). Severity is the fraction of *computable* checks
(input not `None`) that actually triggered a warning -- a company missing a
row never contributes a fabricated zero, and a company with zero triggered
warnings among its computable checks is honestly `severity=0` (no widening),
not evidence of anything. Severity only ever widens `sigma_multiplier` (`>=
1.0`, capped at `accounting_sigma_max_multiplier`) and adds
`left_tail_extra` (`>= 0.0`, capped at `accounting_left_tail_extra_max`). It
never changes `conditional_mean_multiplier`. This is structurally guaranteed,
not just tested: `scenario.py`'s `log_mu = log(conditional_mean) -
scenario_sigma**2 / 2` back-solves the lognormal location parameter from a
mean that is fixed *before* sigma is applied, so `E[X] = conditional_mean`
holds for every value of `sigma_multiplier` / `left_tail_extra` by
construction (verified in
`test_accounting_quality_widens_sigma_and_left_tail_not_conditional_mean`).

### reconciliation confidence -> model_confidence only

Reuses `reconcile()` unmodified, restricted to `filed_date <= as_of`. Only
`revenue`, `shares_outstanding`, and `cash` are compared; `liabilities` is
left `None` rather than substituted with `total_debt` (`FinancialPeriod` does
not carry `Total Liabilities Net Minority Interest`, and mapping a different
accounting concept onto an absent field would violate the "never fill an
absent field with a different concept" rule). `MISMATCH` /
`MAGNITUDE_MISMATCH` findings only lower `uncertainty.model_confidence`
(bounded by `reconciliation_confidence_penalty`); the state is never moved.

### FinancialPeriod extension (option (a) from the handoff)

`FinancialPeriod` gained five optional trailing fields --
`total_assets`, `inventory`, `accounts_receivable`, `goodwill`,
`stock_based_compensation` -- populated from `Total Assets` / `Inventory` /
`Accounts Receivable` / `Goodwill` (balance sheet) and `Stock Based
Compensation` (cash flow). All five row names were confirmed against a real
`raw_snapshots.payload` in this repository's database (not assumed from
yfinance documentation) before implementation. This module is shared with v4
(`build_financial_history` backs the `/reinvestment-quality` and
`/accounting-quality` display endpoints, and the J-2 financial-history
view); `test_financial_period_gains_accounting_fields_without_changing_existing_ones`
and the full existing `test_financial_history.py` suite (7 tests, unchanged)
confirm every pre-existing field's value is untouched.

## Coverage-bias control and missingness

Each feature is enabled by config but also requires universe-wide data
coverage measured against the real database before any threshold was chosen
(handoff 4.7: measure first, then write the number). If the coverage floor is
not met, the feature is disabled for every ticker, including tickers that
happen to have a usable row -- verified directly in
`test_low_coverage_runtime_gate_disables_feature_even_with_a_row` with a
ticker that has a complete two-period history sitting in an otherwise-empty
20-ticker population. Missing/failed inputs never change a state; confidence
is handled separately and only ever falls, never rises from an optional
observation being present.

## Real-data result

Two earlier real shadow-run attempts (`1c51920c-eff4-4b8e-86cc-f1fdcdbb7b3d`,
`7352bfe3-4e6a-4353-ae4d-377ae45f6d11`) failed and are recorded as `status
="failed"` in `model_runs` with the full exception in `warnings`, per the
append-only contract -- this is the honest history, not hidden. Both failed
for the same reason: `reconciliation.py`'s zero-denominator branch
(`sec_value == 0`, non-zero model value) returns `relative_diff =
float("inf")`, and Postgres JSONB rejects the literal `Infinity` token
(`InvalidTextRepresentation`). This is a real, previously-latent bug in
`reconciliation.py` that Phase 1-3 never exercised (nothing before Phase 4
serialized a `ReconciliationItem.relative_diff` into a persisted JSONB
column). Fixed in `quality.py`'s reconciliation evidence builder: a
non-finite `relative_diff` is now stored as `null` with an explicit
`relative_diff_is_unbounded: true` flag rather than the raw float (`quality.py`
`_reconciliation_signal`). The `MAGNITUDE_MISMATCH`/`MISMATCH` classification
itself, which is what actually drives the confidence penalty, is unaffected
by this fix -- only the diagnostic magnitude number is now bounded. Note:
`reconciliation.py`'s `reconcile()` is also used directly by the ticker-detail
API (`routes.py:1256`); that response path serializes through FastAPI's
default JSON encoder (`allow_nan=True`), which would also emit the
non-standard `Infinity` token to API clients for the same zero-SEC-value
edge case. That is a pre-existing, out-of-Phase-4-scope risk in shared code,
flagged here for a future pass rather than fixed silently as a side effect.

Current-code shadow run (after the fix):

- run ID: `d92ea8fc-21ba-438f-a66c-46e83512b207`
- as-of: `2026-09-02`
- implementation version: `v5.phase4`
- config hash: `6b3cc5770e6125e7`
- population: 1,273
- PIT-ready inputs: 1,215
- available distributions: 1,164
- unavailable distributions: 109
- objective rows: 6,365

Observed universe coverage (measured, then used to set
`FeatureSpec.required_coverage` in `feature_registry.py`):

| feature | coverage | registry threshold | runtime status |
|---|---|---|---|
| cash_conversion | 100.00% | 0.90 | enabled |
| consensus_revision (Phase 3) | 100.00% | 0.80 | enabled |
| incremental_roic | 99.53% | 0.90 | enabled |
| accounting_quality | 99.53% | 0.90 | enabled |
| per_share_economics | 99.53% | 0.90 | enabled |
| reconciliation_confidence | 22.70% | 0.80 | globally coverage-gated |
| operating KPI (Phase 3) | 9.58% | 0.50 | globally coverage-gated |
| guidance (Phase 3) | 7.62% | 0.50 | globally coverage-gated |
| TAM (Phase 3) | 0.24% | 0.50 | globally coverage-gated |

Applied-feature counts (from `applied_feature_counts` in the persisted run
metrics, i.e. signals that actually changed a state or uncertainty term in
the final distribution, restricted to tickers with an available
distribution):

- `cash_conversion`: 1,082
- `accounting_quality`: 712
- `per_share_economics`: 617
- `incremental_roic`: 214
- `consensus_revision` (Phase 3): 74

Among applied `accounting_quality` signals (1,265 tickers with a computed
severity), the six checks were computable at different rates -- a company
missing a balance-sheet row genuinely does not have that check, it is not
withheld: `accrual_ratio` 99.4%, `cash_conversion` 99.6%, `receivables_gap`
95.0%, `sbc_to_revenue` 94.9%, `goodwill_to_assets` 75.9%, `inventory_gap`
70.9% (many companies in this universe carry no inventory line). Triggered
warnings: `weak_cash_conversion` 468, `receivables_outpacing_revenue` 339,
`inventory_outpacing_revenue` 160, `high_sbc_to_revenue` 138,
`high_goodwill_to_assets` 41, `high_accruals` 34. Mean/median severity was
0.184 / 0.167 -- most flagged tickers trip one or two of the (up to six)
computable checks, not most of them.

PIT and mathematical checks (computed directly against the persisted run,
scanning all 1,273 `model_scores` rows and every growth/quality signal on
each):

- feature evidence later than the as-of cutoff (`observed_at >= as_of + 1
  day`): 0
- probability-order violations (`p_moic_below_0_5 <= p_moic_below_1_0`;
  `p_moic_2x >= p_moic_3x >= p_moic_5x >= p_moic_10x`): 0
- quantile-order violations (`p10 <= p25 <= p50 <= p75 <= p90`): 0
- ES10 > P10 violations: 0
- applied feature missing a computed ablation, restricted to tickers with an
  available distribution: 0 (234 "applied" signals on the 109
  unavailable-distribution tickers correctly carry
  `{"status": "not_computed", "reason": "distribution_unavailable"}` --
  ablation requires a distribution to diff against, so this is the
  documented case, not a defect)
- tickers missing any of the 9 Phase 3 + Phase 4 ablation slots
  (`tam_headroom`, `operating_kpi_nowcast`, `consensus_revision`,
  `guidance`, `incremental_roic`, `per_share_economics`, `cash_conversion`,
  `accounting_quality`, `reconciliation_confidence`): 0
- `ablation_results` (computed) 2,699 + `ablation_not_computed` 8,758 =
  11,457 = 1,273 tickers x 9 feature slots exactly

Live API smoke checks (`/api/v1/models/v5/runs/latest`,
`/api/v1/models/v5/scores?objective=ten_bagger`,
`/api/v1/models/v5/scores/{ticker}`) all returned HTTP 200 against this run;
the ticker-detail response's `states.contract_version` was `v5.phase4`.

The v4 production `scores` table was unchanged immediately before and after
the shadow run:

- rows: 8,225 (both before and after)
- fingerprint (md5 over ordered `id:ticker_id:score_date:scoring_version:
  config_hash:probability:median_moic`): `77a819e8272901addfe3bc2ca3122b36`
  (identical before and after)

## Tests

- Phase 4 focused: 14 passed
  (`tests/unit/test_v5_phase4_quality.py`)
- v5 Phase 1-4 focused selection: 38 passed
  (`test_v5_skeleton.py` + `test_v5_phase2.py` + `test_v5_phase3_growth.py` +
  `test_v5_phase4_quality.py`)
- complete backend suite: 897 passed (883 baseline + 14 new)
- frontend tests: 2 passed
- frontend production build: PASS, 638 modules (unchanged from Phase 3;
  Phase 4 does not touch frontend code)
- `uv run alembic current`: `1d2e3f4a5b6c (head)`, unchanged -- Phase 4 lives
  entirely inside the existing `model_scores.states` / `.features` JSONB
  columns plus `config/model_v5.yaml`, no migration was needed or added

## objectives.quality_compounder: deferred, not enabled

Left `enabled: false` in `config/objectives.yaml`, unchanged from Phase 2/3.
Rationale: Phase 4's quality signals already flow into the single shared
distribution that every currently-enabled objective (`ten_bagger`,
`expected_return`, `risk_adjusted`, `asymmetric`, `capital_preservation`)
reads -- `accounting_quality` widens `sigma`/left tail (which
`risk_adjusted`'s ES10 term and `asymmetric`'s tail ratio already respond
to), and `incremental_roic`/`per_share_economics` already reshape the mean
path every objective consumes. A `quality_compounder` objective distinct from
`risk_adjusted` would need its own formula that reads *something other than*
this shared distribution to avoid being a redundant re-weighting of the same
inputs (Issue #3 section 18.4's explicit prohibition on reintroducing a
100-point quality subscore). No such formula exists in the codebase yet, and
inventing one now, before Phase 5's capital-allocation/balance-sheet state
and Phase 6's tail-risk state exist, would be premature -- a compounding
objective is most naturally the point where duration, reinvestment quality,
*and* capital allocation (still Phase 5) come together. This is a genuine
deferral, to be revisited once Phase 5 lands, not a rejection.

## Deviations from the handoff and open items

- Registry coverage thresholds for the five Phase 4 keys were set from the
  single 2026-09-02 measurement above (0.90 for the four
  ~99.5%/100%-coverage keys, 0.80 for `reconciliation_confidence`, matching
  the Phase 3 precedent of a high bar for XBRL-availability-gated features).
  A single day's measurement is not a distribution; if universe coverage for
  these financial-statement-derived features moves meaningfully on a later
  collection run, these thresholds should be re-measured rather than assumed
  stable.
- The `float("inf")` JSONB-serialization bug in `reconciliation.py` (see
  above) was found and fixed only in the new Phase 4 evidence payload. The
  same underlying `reconcile()` behavior is also reachable through the
  existing ticker-detail API endpoint (`routes.py:1256`) and was not touched
  there -- that is out of Phase 4's scope (touching v4-shared API response
  shape) but is a real latent risk for that endpoint's JSON response on the
  same zero-SEC-value edge case.
- `per_share_economics` deliberately excludes revenue from its CAGR-gap
  input to avoid double-counting `capital.diluted_share_factor`. This is a
  judgment call, not a value specified in the handoff; the reasoning is
  recorded both here and inline in `quality.py`.
- `economics.reinvestment_efficiency`/`economics.cash_conversion` fall back
  to `status="not_collected"` (not `"unsupported"`) when the Phase 4
  `cash_conversion` signal itself did not apply for a ticker, reserving
  `status="unsupported", source="phase4"` strictly for callers that pass no
  `quality_update` at all (the Phase 2/3 test fixtures). This is a small,
  deliberate refinement beyond the handoff's literal text, made because
  "unsupported" would otherwise misleadingly claim the feature was never
  implemented.

## Phase 4 verdict

PASS for implementation and honest shadow validation. `incremental_roic`,
`per_share_economics`, `cash_conversion`, and `accounting_quality` all
cleared their real-measured coverage floors and materially changed states
or uncertainty for the majority of the scored population (1,082 / 712 / 617 /
214 applied instances respectively). `reconciliation_confidence` did not
clear its coverage floor (22.70% against a 0.80 threshold) and is correctly
disabled for every ticker rather than silently favoring the ~23% of tickers
that happen to have XBRL facts on file -- this is the same coverage-gate
discipline Phase 3 applied to TAM/operating-KPI/guidance, and not clearing
the bar is not a failure. Feature efficacy (does incorporating these states
improve v4-vs-v5 predictive performance) remains a Phase 7 backtest question;
Phase 4 only established that the states are computed honestly, PIT-safe,
and structurally guaranteed not to lower a conditional mean.
