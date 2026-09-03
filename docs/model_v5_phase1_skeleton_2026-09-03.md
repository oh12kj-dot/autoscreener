# Model v5 Phase 1 skeleton evidence (2026-09-03)

Source of truth: GitHub Issue #3, Phase 1. This phase creates an independent
shadow execution path; it does not promote v5 or change the v4 ranking.

## Implemented contracts

- `config/model_v5.yaml` controls enablement, shadow mode, target, reliability,
  uncertainty, scenario weights, and feature flags.
- `config/objectives.yaml` fixes the objective namespace. Only `ten_bagger` is
  enabled in Phase 1; Phase 2 objectives are explicitly disabled.
- `scoring/v5/feature_registry.py` records source, target state, transform,
  winsorization, sector normalization, coverage, freshness, PIT support,
  historical support, reliability, family applicability, and default state.
- `scoring/v5/inputs.py` requires an exact same-date included universe,
  `RawSnapshot.available_from <= as_of`, and `PriceSnapshot.trade_date <= as_of`.
  It has no current-value fallback.
- `model_runs` and `model_scores` are append-only, separate from v4 `scores`.
- Phase 1 distributions are labelled `base_only` and
  `source_model_version=v4`, or explicitly `unavailable`. No v5 state update is
  applied yet, and missing input receives confidence 0 rather than a bad score.
- The daily pipeline stage `model_v5_shadow` runs after Live Intelligence and
  before monitoring. Its failure is non-core and cannot stop v4 production.
- `enabled: false` returns `disabled_by_config` without writing a model run.

## Migration verification

- Previous head: `e9b1c3d5f7a9`.
- New head: `f0a1b2c3d4e5`.
- A fresh 409,202,155-byte PostgreSQL gzip backup was opened and its dump header
  verified before migration.
- Upgrade to `f0a1b2c3d4e5`, downgrade to `e9b1c3d5f7a9`, and re-upgrade to
  `f0a1b2c3d4e5` all completed successfully.

## Real shadow run

Command:

```powershell
uv --cache-dir .uv-cache run python -m autoscreener.cli run-v5-shadow --date 2026-09-02
```

Current-config run ID: `908e8d3f-a6f6-47dd-9fb5-c4439681b3c9`.

- mode/status: `shadow` / `succeeded`
- config hash: `606e81a89236b31e`
- exact included population: 1,273
- PIT input ready: 1,215
- persisted base distributions: 1,165
- persisted unavailable distributions: 108
- confidence range: 0.0--0.5; all 108 unavailable rows have confidence 0
- persisted rows with future raw snapshot dates: 0
- persisted rows with future price dates: 0
- enabled v5 state updates: none

The v4 `scores` table contained 6,944 rows with fingerprint
`60fde834aaf7adddbdd2eb81c11dde55` both before and after the real v5 run.

## Phase 1 verdict

PASS for the Phase 1 definition of done: v5 can execute and persist as a
separate shadow version without changing v4. This is only a skeleton. Its
distribution is deliberately the labelled structural seed; P(loss), multiple
threshold probabilities, expected CAGR, expected shortfall, scenario states,
and objective scores remain Phase 2 work and must not be inferred from this
run.
