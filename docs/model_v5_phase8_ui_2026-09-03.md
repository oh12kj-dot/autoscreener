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

## Phase 9: rollback path verification (live, real DB)

Per Issue #3 section 34, the rollback path was exercised live rather than
inspected only by reading code. No `pytest` process was running during
any of these checks (checked immediately before each).

1. **`enabled: false` writes nothing.** Before:
   `model_runs` count = 29, v4 `scores` count = 8,225. Called
   `run_v5_shadow(2026-09-02, model_config=base_config.model_copy(update=
   {"enabled": False}))` -> returned
   `{"status": "skipped", "reason": "disabled_by_config", "population": 0,
   ...}` with **no** `session.add(ModelRun(...))` ever reached (confirmed
   by reading `engine.py`'s early-return branch, and by the after-counts
   below). After: `model_runs` count = 29 (unchanged), v4 `scores` count =
   8,225 (unchanged).
2. **`mode != "shadow"` raises before writing.** Called
   `run_v5_shadow(2026-09-02, model_config=base_config.model_copy(update=
   {"mode": "live"}))` -> raised `ValueError("v5 shadow runner requires
   mode=shadow")`. `model_runs` count after: 29 (unchanged).
3. Both checks used `ModelV5Config.model_copy(update=...)` in-process
   overrides rather than editing the real `config/model_v5.yaml` on disk --
   confirmed via `git status --porcelain config/model_v5.yaml` (empty)
   throughout -- so there was no window where the real config diverged
   from what the (untouched) 09:00 JST scheduled batch would read.
4. **DB stayed append-only and v4 history intact** -- the v4 `scores` count
   (8,225) was identical before and after every check above; nothing in
   this verification path touches the `scores` table at all (confirmed by
   code inspection: `run_v5_shadow` never imports or queries `Score`
   except read-only via `MoicInputs.from_dict()` on already-written v4
   rows, which none of the disabled/error branches above even reach).

## Phase 9: migration reversibility (offline SQL, not executed against the shared DB)

The shared dev database is also where all of this session's v5 evidence
runs live (29 `model_runs` rows accumulated across Phases 0-8). Actually
running `alembic downgrade` against it would destroy that accumulated
audit trail, so reversibility was instead confirmed by generating (not
executing) the SQL:

- `alembic current` -> `2c4e6f8a1b3d (head)`, single head, matches the tip
  of the local revision chain (`e9b1c3d5f7a9 -> f0a1b2c3d4e5 ->
  1d2e3f4a5b6c -> 2c4e6f8a1b3d`).
- `alembic downgrade --sql 2c4e6f8a1b3d:e9b1c3d5f7a9` -> generated SQL that
  is **exactly** 4 `DROP TABLE` statements (`model_v5_forward_returns`,
  `objective_scores`, `model_scores`, `model_runs`), their indexes'
  `DROP INDEX` statements, and 3 `UPDATE alembic_version SET version_num=
  ...` bookkeeping statements. No `ALTER`, `DROP`, `DELETE`, or `UPDATE`
  referencing any other table appears anywhere in the output.
- `alembic upgrade --sql e9b1c3d5f7a9:2c4e6f8a1b3d` -> grepped for any
  statement touching `scores`/`tickers`/`universe_snapshots`/
  `raw_snapshots`/`price_snapshots` -> zero matches.

This structurally confirms the four v5 migrations are additive-only and
safely reversible without any risk to v4 data, without needing to actually
run the destructive direction against the one shared database this
project has.

## Issue #3 section 36 -- Definition of Done, evaluated item by item

Fetched from the issue (read-only) for this evaluation. Each item is
marked **達成** (achieved), **未達成** (not achieved), or **構造的に不可能**
(structurally impossible right now) -- never achieved-when-it-isn't.

| # | Item (as written in §36) | Status | Basis |
|---|---|---|---|
| 1 | v4 Champion benchmarkが再現可能 | 達成(条件付き) | `run-backtest` reproduces v4's benchmark on demand. v4's own backtest quality has known open issues (`INSUFFICIENT_DATA`, 0% delisting settlement, coverage-bias `REVIEW_REQUIRED` -- Entry 1) that are a separate, pre-existing v4 track, not a v5/Phase 8/9 gap. |
| 2 | v5 Challengerが独立versionとして実装 | 達成 | Separate scoring pipeline (`scoring/v5/*`), separate tables (`model_runs`/`model_scores`/`objective_scores`/`model_v5_forward_returns`), separate config (`config/model_v5.yaml`), never reads/writes v4's `scores` table (Phase 8 evidence + `test_v5_phase8_ui_endpoints.py::test_v5_validation_status_never_touches_v4_scores`). |
| 3 | PIT contractがfeature全体に適用 | 達成 | Every implemented growth/quality/capital/tail signal uses `available_from`/`filed_date`/`observed_at` cutoffs (Phases 3-6). Features not yet PIT-safe (`macro_regime`, `litigation`, `acquisition_competing_risk`) are force-disabled in historical mode by `historical_feature_flags()`, which is the PIT contract working as designed, not a gap. |
| 4 | TAM known bug修正・既存誤値再検証 | **未達成** | `collect_market_opportunity.py`'s `_SCALE` still lacks a `trillion` unit and `delisting_events` still has 592/592 `unknown` `event_type` -- tracked separately in `live_intelligence_ui_gap_handoff_2026-09-01.md` Step 3, not touched this round. This is why `tam_headroom` shows up in this phase's live `coverage_gated_features` warning. |
| 5 | distribution outputsが保存/API/UIで利用可能 | 達成 | `ModelScore.distribution` stored; `/models/v5/scores`, `/models/v5/scores/{ticker}` APIs; `V5RankingSection`/`V5TickerDetailSection` render it (this phase). |
| 6 | P10x以外の目的関数を同一distributionから計算 | 達成 | 5 enabled objectives computed from the same per-ticker distribution (live-verified on PACS: `ten_bagger`/`expected_return`/`risk_adjusted`/`asymmetric`/`capital_preservation` all present with distinct values from one `ModelScore` row). |
| 7 | missingnessとconfidenceが分離 | 達成 | Structural rule enforced since Phase 2-6: missing data moves `confidence` (bounded +/-0.20), never a state value. |
| 8 | Live intelligenceの主要signalが適切なfuture stateへ接続 | 達成(配線は完了、実効果は限定的) | All ~20 signals wired into `engine.py`'s growth/quality/capital/tail updates. Today's live evidence run shows 11 of them universe-wide coverage-gated off (`coverage_gated_features:...` warning on every scored row) -- this is the coverage-gate mechanism working as designed against real (currently thin) collected data, not an implementation defect. |
| 9 | v4/v5同日比較backtest | 達成 | `backtest/v5_comparison.py`'s same-day cross-sectional comparison (Phase 7), independently re-verified this round (Spearman 0.907 reproduced by the coordinator). |
| 10 | ablation / bootstrap / regime / bias監査 | 分割評価 | Ablation: 達成 (leave-one-out, now UI-exposed). Bootstrap: infrastructure built (`date_block_bootstrap_ci()`) but blocked -- needs >=30 distinct evaluation dates, only 9 exist. Regime layering and bias audit: **構造的に不可能** -- both require realized returns to stratify/audit against, and `realized_forward_validation_count=0` (confirmed live this round); the Phase 7 coordinator's own review explicitly endorsed not attempting these while that precondition is unmet. |
| 11 | forward shadow validation | 達成(基盤のみ、結果はまだ無い) | `model_v5_forward_returns` + `run_forward_validation_v5()` exist and run; 0 matured observations exist yet because no scored ticker has reached `target_horizon_years` -- a time-gated structural limit, not a code defect. |
| 12 | Validation UIでChampion/Challenger比較 | 達成(このラウンドで実装) | `V5ValidationSection` on `ValidationPage.tsx`. |
| 13 | Ranking UIでModelとObjectiveを切替可能 | 達成(このラウンドで実装) | Model toggle + objective `<select>` restricted to config-enabled objectives on `RankingPage.tsx`. |
| 14 | v5の「なぜ」がstate shiftとして説明可能 | 達成(このラウンドで実装) | `V5TickerDetailSection`'s ablation rendering, verified against real `PACS` data above. |
| 15 | migration upgrade/downgrade検証 | 達成 | Verified via offline SQL generation this round (see above) -- full round trip confirmed additive-only and reversible without touching any v4 table. |
| 16 | backend tests全通過 | 達成 | 941 passed (>= the 938 baseline this round started with). |
| 17 | frontend tests全通過 | 達成(母数は薄い) | 2/2 passed -- the entire frontend unit-test suite is 2 tests; this phase relied on TypeScript + production build + backend endpoint tests for its own coverage rather than adding frontend unit tests, so "全通過" is true but the safety margin it provides is thin. |
| 18 | frontend production build成功 | 達成 | `npm run build` succeeded, 641 modules, 0 type errors. |
| 19 | scheduled pipeline smoke成功 | **未達成(意図的)** | Every phase of this multi-round effort has deliberately avoided starting, stopping, or invoking the 09:00 JST scheduled daily pipeline, per an explicit standing rule repeated in every round's instructions. `forward_validation_v5` remains in `RESERVED_STAGE_NUMBERS`, not wired into `PIPELINE_STAGE_SEQUENCE`, specifically because it has not been smoke-tested inside that pipeline. This item cannot be marked achieved without violating the standing rule; it is an open item for whoever is authorized to run that smoke test. |
| 20 | 実DB coverage確認 | 達成 | Measured repeatedly with real numbers: universe/raw snapshot date ranges (Phase 7), feature coverage-gate warnings on real scored rows (Phase 8, this round). |
| 21 | Promotion Decision Record作成 | 達成 | `docs/model_v5_validation.md`, Entry 1 (Phase 7) and Entry 2 (this round), both `CONTINUE_SHADOW`. |
| 22 | rollback経路確認 | 達成(このラウンドで実施) | Live-verified this round: `enabled: false` -> zero writes; `mode != shadow` -> raises before writing; v4 `scores` count unchanged throughout (see above). |

**Summary: 15 of 22 achieved, 2 not achieved (items 4, 19 -- both
pre-existing, separately tracked, and explicitly not attempted this round
for stated reasons), 2 structurally impossible right now (regime
stratification and bias audit within item 10, both gated on realized
returns that do not exist yet), 1 partially achieved with an explicit
caveat about margin (item 17), 1 achieved-but-caveated (item 1, v4's own
backtest quality), 1 achieved-with-limited-real-effect (item 8, coverage
gating suppressing 11/20 signals against today's thin real data).** None
of the 22 items is marked achieved where it is not; the point of this
table is that a reader can tell the difference between "done" and
"correctly not yet done" at a glance.
