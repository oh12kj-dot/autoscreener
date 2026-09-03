# Model v5 Phase 0 baseline (2026-09-02)

This is the evidence record for GitHub Issue #3 Phase 0. It is not a v5
promotion decision. All timestamps and counts below are tied to main commit
`fc98a3aaac7b3660bddde009adbf81d58cb3e62d` plus the explicitly listed local
Phase 0 changes.

## Repository and schema

- Initial `HEAD`, local `main`, and `origin/main` inspected before Phase 0 work:
  `fc98a3aaac7b3660bddde009adbf81d58cb3e62d`.
- While the real pipeline was running, local `main` and `origin/main` advanced to
  `7f057906a6fc5f2f1d25c40d89061fdd9a415fad` (`feat: model v5 Phase 0
  data-quality corrections and baseline record`). The validation below uses
  that current checkout; no pre-existing worktree change was discarded.
- Existing local changes preserved: delisting false-positive recovery in
  `collect_delistings.py`, `delisting_source.py`, `cli.py`, and their tests.
- Alembic current/head: `e9b1c3d5f7a9` / `e9b1c3d5f7a9`.
- Backend regression after the pipeline-health repair: 859 passed.
- Frontend: 2 tests passed; production build passed. Lint exited 0 with 15
  pre-existing React warnings.

## v4 champion baseline

Both runs persist `scoring_version=v4`, config hash `eb5be5480866aab3`, the
full config snapshot, rebalance dates, metrics, and calibration map in
`backtest_runs`.

| Run | Overlap | Observations | Evaluation dates | Effective dates | Lift | Rank IC | Worst-date lift | Delisted settlement | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 33 | yes | 2,229 | 7 | 5.31 | 1.270 | 0.129 | 0.488 | 0.0% | all KPI `INSUFFICIENT_DATA` |
| 34 | no | 468 | 2 | 1.21 | 1.393 | 0.165 | 1.427 | 0.0% | all KPI `INSUFFICIENT_DATA` |

The baseline is reproducible but not accepted for investment use. It contains
zero observed delisting settlements, has too few effective evaluation dates,
and the overlapping portfolio simulation is materially negative. A higher
ranking metric alone must not be used to promote a challenger.

## Data-quality corrections

### Delisting false positives

The pre-existing local repair rejects a Form 25/15 issuer-level match when the
same ticker traded more than 30 days after the claimed delisting date. The
rollback is dry-run by default and can be symbol-scoped. Real DB recovery found
the false-positive population left 498 non-delisted rows quarantined with zero
consecutive failures; a fresh 366,832,014-byte backup was created before those
498 quarantine flags were cleared. The 94 unresolved rows were retained.

### TAM

- Added explicit `trillion = 1e12` parsing.
- SEC matches without a scale are rejected instead of being multiplied by 1.
- Enforced positive TAM, ISO-like three-letter currency, absolute and
  TAM/revenue magnitude checks, and `TAM >= addressable revenue`.
- Penetration is calculated only when TAM and revenue currencies are both
  explicit and equal.
- Machine extraction confidence is forced to `low`.
- Existing rows were revalidated: 5 total, 4 valid, 1 invalid. XERS `$1.0`
  without a scale was archived to `live_dataset_coverage` and deleted. A real
  recollection rejected the same match and did not recreate it.

### M&A and macro

- `event_type=unknown` is excluded from the acquisition-rate denominator.
  The API now returns classification coverage and disables historical use below
  80% classified coverage. The live DB currently has 94/94 `unknown`, so the
  acquisition share is `null`, not 0%.
- All 8,673 pre-run macro exposure rows explicitly had
  `fred_vintage_supported=false`. The collector/API now also expose
  `historical_backtest_supported=false` and `forward_shadow_only=true`.

## Coverage-bias audit

The reproducible command is:

```powershell
uv --cache-dir .uv-cache run python -m autoscreener.cli audit-coverage-bias
```

For the final 2026-09-02 v4 ranking (776 measurable scores), status is
`REVIEW_REQUIRED`:

- Spearman(`v4 probability`, Live datasets with data): 0.825.
- Top-decile mean datasets with data: 3.935 versus 1.798 overall (+2.137).
- Largest dataset coverage-rate gap: macro exposure +54.6 percentage points.

This does not mean v4 reads Live Intelligence; it does not. It proves that the
currently tracked/collected population is strongly selected toward high-ranked
companies. Every v5 comparison therefore needs coverage stratification or
reweighting, and missingness must never create a score advantage.

## Pipeline acceptance

Post-recovery full daily pipeline run `fed7a34c-4d22-4b5a-8546-47eda3ad39bf`
completed against 5,799 real symbols (2026-09-02 23:36--2026-09-03 04:19
JST). The command exited 0, all applicable stages completed, and the backup
stage wrote a 409,188,106-byte gzip. The immutable run status is `degraded`
because investment-intelligence extraction recorded two failures:

- Collection: 4,650 success, 1,121 sanitized, 25 empty responses, 3 parse
  failures, and 0 new quarantines.
- Consensus: 5,889 processed, 1,408 inserted, 0 failed.
- Gates: 1,273 included, 4,497 excluded, 25 no-data, 94 delisted.
- Scoring: 776 measurable, 389 negative-outlook, 108 unmeasurable.
- Filing and Live Intelligence stages completed; market opportunity had 2/299
  with data and macro exposure 290/299 with data.
- The two failures were CDNA and MYRG. Reproduction exposed malformed inline
  XBRL table text concatenating monetary cells into impossible debt principals
  (`3.8e24` and `1.6e39` USD), overflowing `NUMERIC(24,4)`. The debt extractor
  now rejects non-positive or greater-than-`1e15` values and logs/persists the
  full failure detail. A real targeted rerun succeeded for both tickers, wrote
  266 valid rows, and reported zero failures.

Post-run database checks:

- 5,893 tickers; 94 delisted and the same 94 quarantined (the recovered 498
  false positives remained active).
- Latest v4 score set: 1,165 rows, including 776 measurable scores.
- TAM: 4 rows, zero invalid magnitudes, zero cross-currency penetration values.
- M&A/delisting history: 94 events, all `unknown`; historical acquisition rate
  remains unsupported rather than being reported as 0%.
- Macro exposure: 9,543 snapshots, zero with vintage support; all 870 rows from
  this run explicitly carry historical-unsupported/forward-shadow flags.

## Phase 0 verdict

The implementation and evidence collection for Phase 0 are complete, and v4
is reproducible. This is **not a clean acceptance for model promotion**: the
full run remains truthfully `degraded` as immutable history, v4 backtests remain
`INSUFFICIENT_DATA` with zero delisting settlements, scoring warned that the
current v4/config hash has no calibration map, and the coverage audit is
`REVIEW_REQUIRED`. Phase 1 should therefore begin only as an offline shadow
challenger; it must not replace the v4 champion or be presented as
investment-grade until those acceptance gaps are closed.
