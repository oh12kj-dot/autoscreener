# Model v5 Phase 8 — Frontend UI (2026-09-03)

Source of truth: GitHub Issue #3 sections 28/29/34/36, via
[model_v5_phase4_handoff_2026-09-03.md](model_v5_phase4_handoff_2026-09-03.md).
Phase 7 re-audit fixes (historical-run metadata persistence, pipeline
stage-count correction, DB-measurement-during-tests methodology note) were
committed separately as `9d38faa` and are not repeated here.

**Before this phase, the frontend had zero references to v5.** This phase
adds a v5 surface to three existing pages, entirely additively: nothing in
any existing v4 component, hook, or JSX branch was modified or deleted.

## Operational boundary

The 09:00 JST Windows-scheduled daily pipeline was not started, stopped,
or restarted for this work. Nothing was pushed to any remote, and nothing
was posted to GitHub Issue #3. All DB reads below were taken with no
`pytest` process running (checked via `ps aux | grep pytest` immediately
before each measurement), per the Phase 7 re-audit's instruction that this
applies to **every** DB aggregate/measurement, not only v4-fingerprint
checks.

## What was built

### 1. Backend: two new read-only endpoints (`src/autoscreener/api/routes.py`)

- `GET /api/v1/models/v5/objectives` — returns only
  `ObjectiveDefinition.enabled=True` objectives from
  `config/objectives.yaml`. `quality_compounder` and `execution_adjusted`
  are disabled there and therefore **structurally cannot** appear in this
  list — the frontend's objective `<select>` is populated from this
  endpoint, so there is no way for the UI to offer them, independent of
  any frontend-side filtering logic.
- `GET /api/v1/models/v5/validation-status` — live-computed (never
  hardcoded): `evaluation_dates_count` / `evaluation_date_range` from
  `DISTINCT universe_snapshots.snapshot_date WHERE included`,
  `realized_forward_validation_count` from
  `COUNT(model_v5_forward_returns WHERE realized_return IS NOT NULL)`,
  `unsupported_historical_features` from
  `FEATURES_BY_KEY WHERE historical_backtest_supported = False`, and the
  latest `v5` `ModelRun` row. `decision`/`decision_entry_date` mirror
  `docs/model_v5_validation.md`'s current Decision Record entry (manually
  synchronized, documented as such in the endpoint's docstring).

Both are covered by `tests/unit/test_v5_phase8_ui_endpoints.py` (3 new
tests), including an explicit assertion that calling
`/models/v5/validation-status` does not change `Score` (v4's table) row
count — i.e. the endpoint is read-only with respect to v4.

### 2. Frontend: three new additive components

- `frontend/src/components/V5RankingSection.tsx` — a **fully separate**
  ranking table (does not import or reuse any v4 `RankingPage` rendering
  code), driven by `fetchV5Objectives` / `fetchV5ValidationStatus` /
  `fetchV5Scores`. Columns: rank / ticker / selected-objective value /
  P(10x) / expected CAGR / P(loss, MOIC<1.0) / survival-adjacent
  confidence / model version / warnings. The objective `<select>` is
  populated exclusively from `/models/v5/objectives`.
- `frontend/src/components/V5TickerDetailSection.tsx` — renders
  `features.ablation` from `/models/v5/scores/{ticker}`. For each
  leave-one-out entry: `status: "computed"` entries are formatted as
  `<label>: without → with (Δ...)` — for `growth_duration_years` and
  `initial_growth_rate` the "with" value is read back from
  `states.growth.duration_years.value` / `states.growth.initial_rate.value`
  and "without" is derived as `with - state_shift[key]`; the other four
  `state_shift` keys (`revenue_multiple_ratio`, `sigma_multiplier`,
  `left_tail_extra`, `survival_multiplier`, `model_confidence`) do not
  have a directly comparable absolute field elsewhere in the payload, so
  they are shown as a signed delta only, never invented. `status:
  "not_computed"` entries always render their `reason` string (e.g.
  `runtime_disabled_low_coverage`) — never a blank cell. A v4-vs-v5
  comparison table shows P(target) and expected MOIC for both models
  (v4 values passed in from `CandidateDetail`, already loaded by the page)
  plus rank for both — v5 rank comes from the ticker's
  `ObjectiveScore.rank` for the default objective; v4 rank has no existing
  per-ticker endpoint, so it is computed on demand (button, not on page
  load) via two `/candidates` calls (`limit=1` to get `total`, then
  `limit=total`) to avoid paying that cost on every ticker-detail view.
- `frontend/src/components/V5ValidationSection.tsx` — reads
  `/models/v5/validation-status` and renders champion/challenger,
  decision, last run, PIT-evaluated date count, realized
  forward-validation count, and unsupported-feature list. When
  `realized_forward_validation_count === 0` it renders an explicit
  `データ不足(実現済み観測 0件・INSUFFICIENT_DATA)` message rather than a
  bare `0`, with a one-sentence explanation of *why* (evaluation window
  hasn't reached `target_horizon_years` for any scored ticker yet).

All three components carry visible `forward_shadow_only` /
`not_for_production` badges (`.v5-badge`, styled with the existing
`--danger` token — no new color tokens added).

### 3. Wiring into existing pages (additive only)

- `RankingPage.tsx`: added a `model` URL param (`?model=v5`, mirroring the
  existing `target`-in-URL pattern) and a toggle. When `model !== "v5"`
  the **entire pre-existing v4 JSX subtree renders byte-for-byte
  unchanged** (verified by diff: the only change inside that branch is
  its enclosing `<>...</>` wrapper). The v4 data-fetching `useEffect`
  still runs even when `v5` is selected (Rules of Hooks — hooks can't be
  conditionally skipped); this is an accepted redundant fetch, not a
  behavior change, since its result is simply not rendered in that case.
- `TickerDetailPage.tsx`: `<V5TickerDetailSection>` appended after the
  existing `<LlmAnalysisSection>` (last rendered element), receiving
  `detail.probability` / `detail.expected_moic` — already-loaded v4 values
  — as props for the comparison table.
- `ValidationPage.tsx`: `<V5ValidationSection>` inserted after the
  existing "順位はリスクをほとんど反映していません" notice, before the
  v4 backtest KPI table.

## Verification

### Tests

- Backend: `uv run pytest -q` → **941 passed** (up from the 938 baseline
  at the start of this round; +3 from
  `tests/unit/test_v5_phase8_ui_endpoints.py`). No `pytest` process was
  running during any of the DB measurements below.
- Frontend unit tests: `npm test -- --run` → **2 passed** (pre-existing
  suite; this phase added no new frontend unit tests, relying on the
  TypeScript compiler + production build + backend endpoint tests for
  coverage of the new code paths).
- Frontend production build: `npm run build` (`tsc -b && vite build`) →
  succeeded, 641 modules transformed, no type errors.
- Frontend lint: `npm run lint` (oxlint) → exit 0, 17 warnings, **zero
  errors**. All 17 warnings are the pre-existing `set-state-in-effect`
  pattern already present in ~13 untouched v4 files (e.g.
  `AlertsPage.tsx`, `WatchlistPage.tsx`, `PipelinePage.tsx`); the two new
  files that trigger it (`V5RankingSection.tsx`,
  `V5TickerDetailSection.tsx`) follow the exact same pre-existing
  fetch-in-`useEffect` convention as the rest of the codebase, not a new
  pattern.

### Commit → clean tree → evidence run

Commit `64f185bd18e69a2c53d918bd64f907f0828d8be8` (`feat: model v5 Phase 8
UI ...`), working tree clean (`git status --porcelain` empty)
immediately before every run below.

Two evidence runs were taken via `run_v5_shadow(...)`, both with no
`pytest` process running:

1. `as_of=2026-09-03` (today) → `run_id=d2d44621-1990-450d-9e21-fc536812a0e9`,
   `population_count=0` — today's `universe_snapshots` row does not exist
   yet (the daily pipeline that would populate it was correctly **not**
   triggered by this work). `code_revision = {dirty: false, commit:
   64f185bd18e69a2c53d918bd64f907f0828d8be8}`.
2. `as_of=2026-09-02` (latest date with `included` universe data) →
   `run_id=bc09ee7f-e3d0-41cd-a742-229288f82d75`:

   | metric | value |
   |---|---:|
   | population | 1,273 |
   | input_ready | 1,215 |
   | base/phase2..6 distributions | 1,164 each |
   | empty_distributions | 109 |
   | objective_scores | 6,365 |
   | ablation_results | 2,568 |

   `config_hash=1b7b6801090eae5c`, `code_revision = {dirty: false, commit:
   64f185bd18e69a2c53d918bd64f907f0828d8be8, reason: null}`,
   `enabled_objectives = [asymmetric, capital_preservation,
   expected_return, risk_adjusted, ten_bagger]` (exactly the 5
   config-enabled objectives — `quality_compounder` and
   `execution_adjusted` are absent, confirming the coverage-gate-by-config
   behavior the ranking-page objective selector depends on),
   `default_objective=ten_bagger`.

### Live endpoint exercise against the `2026-09-02` evidence run

- `GET /models/v5/objectives` → 200, 5 objectives listed (`ten_bagger`,
  `expected_return`, `risk_adjusted`, `asymmetric`,
  `capital_preservation`), `default_objective=ten_bagger`. Matches the
  run's `enabled_objectives` exactly.
- `GET /models/v5/validation-status` → 200,
  `evaluation_dates_count=9`, `evaluation_date_range=[2026-08-23,
  2026-09-02]`, `realized_forward_validation_count=0`,
  `unsupported_historical_features=[acquisition_competing_risk,
  litigation, macro_regime]`, `warnings` includes
  `forward_validation_zero_matured_observations` — these are the same
  Phase-7-measured numbers (9 evaluation dates, 0 realized returns),
  confirmed still current and now surfaced through the API the frontend
  actually calls, not just internal test assertions.
- `GET /models/v5/scores?as_of=2026-09-02&objective=ten_bagger&limit=3` →
  200, `total=1273`, top row `PACS` (`objective_value=0.0682`,
  `p_target=0.0682`, `expected_cagr=0.1853`, `confidence=0.5`, warnings
  include `coverage_gated_features:capital_allocation,
  customer_concentration,debt_maturity,future_dilution_capacity,guidance,
  liquidity,litigation,macro_regime,operating_kpi_nowcast,
  reconciliation_confidence,tam_headroom` — i.e. 11 of this run's
  features were universe-wide coverage-gated off, and the warning says so
  explicitly on every affected row).
- `GET /models/v5/scores/PACS?as_of=2026-09-02` → 200. Sample
  `features.ablation` entries confirm both code paths the TickerDetail
  component exercises:
  - `status: "not_computed"`, e.g. `guidance` →
    `{"reason": "runtime_disabled_low_coverage"}` (coverage-gated,
    reason present, never blank).
  - `status: "computed"`, e.g. `incremental_roic` → `state_shift:
    {growth_duration_years: -0.02557, revenue_multiple_ratio: -0.00487,
    ...}`, and `states.growth.duration_years.value = 3.0237` (the "with"
    value), so the component renders `growth duration: 3.0y → 3.0y (Δ-0.0y
    at this precision)` for this particular ticker/feature pair — a small
    real effect, not the illustrative `3.8y → 5.2y` figure from the
    handoff (that was a format example, not a claim about any specific
    ticker).
  - `objectives` array includes `rank` per objective (e.g.
    `ten_bagger rank=1` for PACS at this `as_of`), which is what
    `V5TickerDetailSection` reads for the "v5 rank" comparison cell.

## What this phase does not claim

- It does not claim v5 is validated for production use — the UI itself
  says the opposite everywhere v5 output appears
  (`forward_shadow_only`/`not_for_production` badges,
  `INSUFFICIENT_DATA`-style messaging on `ValidationPage`).
- It does not claim the v4-rank-lookup button is cheap at full universe
  scale forever; at the current ~1,273-ticker population a `limit=total`
  fetch is a single extra request triggered only on demand, but this
  would need revisiting if the universe grows by an order of magnitude.
- It does not add a "v4 rank" value automatically on page load — that is
  a deliberate cost/staleness tradeoff (see above), not an oversight.
