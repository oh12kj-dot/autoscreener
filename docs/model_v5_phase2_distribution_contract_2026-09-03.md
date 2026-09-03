# Model v5 Phase 2 — Distribution Contract (2026-09-03)

Source of truth: GitHub Issue #3, Phase 2.

## Scope and operational boundary

The scheduled daily batch was not invoked. It starts at 09:00 JST, so Phase 2
validation used the dedicated `run-v5-shadow` command against the completed
2026-09-02 PIT universe. The v4 `scores` table remained the production champion.

## Implemented contract

- Typed future-state objects cover growth, economics, capital structure,
  valuation, competing risk, and uncertainty.
- States belonging to later phases are stored as `unsupported` with `null`
  values. Missing inputs are never converted to zero.
- The v4 structural result is only a labelled seed. Phase 2 transforms it into
  a three-scenario lognormal mixture plus an explicit failure atom.
- Lower confidence widens sigma without lowering the conditional mean.
- Scenario means are normalised to preserve the seed conditional expectation.
- The downside scenario uses the configured heavier-tail multiplier.
- Survival is held constant until the balance-sheet/tail-risk phases.
- Acquisition probability remains `null` until competing-risk support exists.

The distribution returns:

- `P(MOIC < 0.5)` and `P(MOIC < 1.0)`
- `P(MOIC >= 2x/3x/5x/10x)` and the configured-target probability
- expected/median MOIC and CAGR
- lower-tail expected shortfall at 10%
- P10/P25/P50/P75/P90 MOIC
- survival, acquisition support state, confidence, and scenario parameters

Expected MOIC includes the failure atom. ES10 is the mean of the lowest 10% of
the full mixture and is calculated analytically from truncated lognormal first
moments after numerically solving the mixture quantile.

## Objectives and persistence

`objective_scores` stores one append-only row per run/ticker/enabled objective.
Distribution-only objectives enabled in Phase 2 are:

- ten-bagger probability
- expected CAGR
- risk-adjusted expected CAGR
- right-tail/left-tail asymmetry
- capital preservation

Quality-compounder and execution-adjusted remain disabled because their required
states arrive in later phases. Null distributions receive null objective values
and no rank. The API exposes the latest successful run, an objective-ranked list,
and per-ticker state/distribution/objective detail under `/api/v1/models/v5/`.

## Migration verification

- prior revision: `f0a1b2c3d4e5`
- Phase 2 head: `1d2e3f4a5b6c`
- downgrade to prior revision: PASS
- re-upgrade to head: PASS

## Real-data shadow verification

Current-code run:

- run ID: `7dc3d72d-2402-4480-97bf-ab27760bab11`
- as-of: `2026-09-02`
- config hash: `4e88b9abac70ce92`
- population: 1,273
- PIT-ready inputs: 1,215
- available Phase 2 distributions: 1,165
- unavailable distributions: 108
- objective rows: 6,365 (1,273 × 5)
- ranked rows per objective: 1,165
- unranked unavailable rows per objective: 108

All 1,165 available rows contained the required output fields. Across real rows:

- probability-order violations: 0
- quantile-order violations: 0
- ES10 greater than P10 violations: 0
- configured 10x target versus `p_moic_10x` mismatches: 0

Live API smoke results were HTTP 200 for latest-run, ranked-list, and ticker-detail
endpoints. The returned latest run was the ID above, list total was 1,273, and
the detail contract version was `v5.phase2` with five objective results.

The v4 table fingerprint immediately before and after the Phase 2 shadow run was
unchanged:

- rows: 8,225
- fingerprint: `cd3129d07f91dd3f66704ec759b7f2bc`

This count is the Phase 2 start-of-work database state; it is intentionally not
compared to an older Phase 1 count because other existing operations had already
added v4 rows before this phase began.

## Test evidence

- Phase 2 plus API regression selection: 68 passed
- complete backend suite: 873 passed
- frontend tests: 2 passed
- frontend lint: exit 0 (15 pre-existing React warnings)
- frontend production build: PASS, 638 modules
- Python compileall: PASS

## Phase 2 verdict

PASS. The Issue #3 Phase 2 completion outputs are available from an independent
v5 shadow run and typed API, while v4 remains unchanged. Phase 3 growth/TAM/KPI/
expectations signals are not yet applied and the challenger remains explicitly
not for production.
