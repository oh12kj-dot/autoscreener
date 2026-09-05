# 2026-09-05 audit follow-up: three independent defects

Fixes three defects found in the 2026-09-05 audit of `main` (HEAD `4d92fbc`
at the time of the audit). Background: `docs/racr_integrated_redesign_plan_
2026-09-04.md` §1, `docs/racr_wp_a_operational_safety_2026-09-04.md`,
`docs/racr_wp_b2_risk_terms_2026-09-04.md`.

## Defect 1 — alembic had no test-database isolation

**Hazard.** `tests/conftest.py`'s `_require_isolated_test_database` (WP-A)
fails pytest closed unless `TEST_DATABASE_URL` names a database ending in
`autoscreener_test`. `alembic/env.py` had no equivalent guard: it set
`sqlalchemy.url` straight from `get_settings().database_url`, which never
looks at `TEST_DATABASE_URL` at all. On 2026-09-05 a session ran

```
TEST_DATABASE_URL=...autoscreener_test uv run alembic upgrade head
```

intending to migrate the test database. Because `DATABASE_URL` itself was
not also set, alembic silently resolved and migrated the **dev** database
instead. Harmless that time, but the same class of accident WP-A already
closed for pytest.

**Fix.** New module `src/autoscreener/db/migration_guard.py`:
`resolve_alembic_database_url(resolved_url, test_database_url)` — a small
pure function. Behavior:

- `TEST_DATABASE_URL` unset → returns `resolved_url` unchanged (the
  ordinary dev-migration path, byte-for-byte the old behavior).
- `TEST_DATABASE_URL` set **and** `resolved_url`'s database name already
  ends in `autoscreener_test` → no conflict, returns `resolved_url`
  unchanged.
- `TEST_DATABASE_URL` set **but** `resolved_url` names a database that does
  *not* look like a test database → raises
  `AmbiguousMigrationTargetError` with a message naming both databases and
  the two correct invocations. Never silently prefers one signal over the
  other — this is the exact case that produced the 2026-09-05 accident.

`alembic/env.py` now calls this before `config.set_main_option(
"sqlalchemy.url", ...)`, with a comment recording the incident and the
correct invocation for each database:

```
Dev database:  uv run alembic upgrade head
Test database: DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
```

(`TEST_DATABASE_URL` alone still does nothing for alembic — by design,
matching how pytest's own guard is a separate mechanism — but now a stray
`TEST_DATABASE_URL` left set in the shell aborts alembic instead of being
silently ignored.)

**Verification performed.**
- `uv run alembic current` with no `TEST_DATABASE_URL` set → succeeds,
  reports `b3f6d1a08c92 (head)` (dev DB, unaffected).
- `DATABASE_URL=<test url> uv run alembic current` → succeeds, same head
  (the documented correct test-DB invocation).
- `TEST_DATABASE_URL=<test url> uv run alembic current` (reproducing the
  2026-09-05 command exactly) → now aborts with
  `AmbiguousMigrationTargetError` and the corrective message, instead of
  silently touching the dev DB.
- `tests/unit/test_alembic_migration_guard.py` (7 tests): the four
  `resolve_alembic_database_url` behaviors above, an error-message content
  check, and a wiring check that `alembic/env.py` still routes through the
  guard (greps for `resolve_alembic_database_url` and
  `os.environ.get("TEST_DATABASE_URL")`) so a future edit can't quietly
  revert to the unguarded assignment.

**Files:** `src/autoscreener/db/migration_guard.py` (new),
`alembic/env.py`, `tests/unit/test_alembic_migration_guard.py` (new).

---

## Defect 2 — frontend dev server could silently drift to a CORS-rejected port

**Hazard.** `frontend/vite.config.ts` did not pin a port. Vite's default
behavior is "listen on 5173, and if that's taken, silently move to 5174,
5175, ...". `src/autoscreener/api/main.py`'s CORS `allow_origins` is a
hardcoded list containing only `http://localhost:5173` and
`http://127.0.0.1:5173`. A dev server that drifted to 5174 would fail CORS
on every request, surfacing to a developer only as a generic "can't
connect" error. This was live on the audit machine on 2026-09-05: both
5173 and 5174 were simultaneously listening.

**Fix.**
- `frontend/vite.config.ts`: added `server.port: 5173` and
  `server.strictPort: true`, so a taken port fails the dev server
  immediately instead of silently relocating it.
- `src/autoscreener/api/main.py`'s CORS block and `vite.config.ts` each got
  a comment pointing at the other file, spelling out that the port number
  is two independent hardcoded literals with no shared source of truth,
  and that both must move together.
- README's CORS troubleshooting row was rewritten to describe the actual
  current behavior (dev server now fails to start on a port collision
  rather than silently moving) and the fix (free port 5173).
- Did **not** widen the CORS allowlist to include 5174 — that would treat
  the symptom and double the allowed-origin surface for every future port
  collision, per the task's explicit instruction.

**Tests added (mechanical tie, not just comments).**
- `tests/unit/test_cors_vite_port_alignment.py` (2 tests, Python): reads
  both `api/main.py` and `vite.config.ts` as text and asserts (a) the Vite
  config pins a port and sets `strictPort: true`, and (b) every
  localhost/127.0.0.1 CORS origin carries exactly the pinned Vite port —
  fails if the two files are ever edited to disagree.
- `frontend/src/viteConfig.test.ts` (2 tests, vitest): imports the Vite
  config object directly and asserts `server.port === 5173` and
  `server.strictPort === true`.

**Verification performed.**
- `npm run build` → clean (tsc -b + vite build), matches baseline.
- `npm test -- --run` → 3 files / 6 tests passed (baseline 4 tests + 2 new
  in `viteConfig.test.ts`).
- `npm run lint` → 17 warnings / 0 errors, unchanged from baseline.
- `TEST_DATABASE_URL=... uv run pytest tests/unit/test_cors_vite_port_alignment.py`
  → 2 passed.

**Files:** `frontend/vite.config.ts`, `frontend/src/viteConfig.test.ts`
(new), `src/autoscreener/api/main.py`, `README.md`,
`tests/unit/test_cors_vite_port_alignment.py` (new).

---

## Defect 3 — `expected_shortfall_10pct_log` degeneracy

**Hazard.** `expected_shortfall_10pct_log` is computed at a fixed 10%
probability quantile. Every real ticker's failure atom already exceeds
10%, so the quantile falls exactly on the failure-atom floor and the field
is byte-for-byte the same constant (`-0.657881455`) for the entire
universe (`docs/racr_shadow_run_diagnostic_2026-09-04.md` §3.1). WP-B2
replaced its role inside the `risk_adjusted_compounding` objective with
`expected_shortfall_10pct_log_given_survival`, but the old field stayed in
the distribution contract, the DB, and the API response, and the per-run
diagnostic reported it every run as an unexplained constant.

**Investigation (which option applies).** Grepped the whole repo (`src`,
`frontend/src`, `tests`) for `expected_shortfall_10pct_log`. Findings:

- `src/autoscreener/scoring/v5/objectives.py`: does **not** read the old
  field — `risk_adjusted_compounding` reads
  `expected_shortfall_10pct_log_given_survival` only (WP-B2's fix already
  landed). The old field is not a live decision input anywhere in the
  scoring code.
- `src/autoscreener/api/schemas.py`: field is part of the public API
  contract (`ModelV5DistributionView`).
- `frontend/src/api/types.ts`: typed field on the client.
- `frontend/src/components/V5TickerDetailSection.tsx` (line 281-282, pre-fix)
  and `frontend/src/v5Labels.ts`: the field **was actively rendered** in
  the ticker detail page, as its own labeled row ("下位10%期待損失(年率
  log)"), with no indication to a user that it is a degenerate constant.
- `tests/unit/test_v5_racr_wp_b.py` and `tests/unit/test_v5_racr_wp_b2.py`:
  multiple tests assert on the old field's exact byte-for-byte-unchanged
  behavior (it is deliberately preserved for backward compatibility, per
  WP-B2's own code comments in `distribution.py`/`objectives.py`).

**Decision: deprecate, do not remove.** The field fails the "nothing reads
it" bar for removal — it is rendered directly to end users in the ticker
detail UI, and tests pin its historical behavior on purpose. Removing it
would mean deleting a user-facing UI row, the API field, and rewriting/
deleting the WP-B2 backward-compatibility tests, all to eliminate a field
that costs nothing to keep now that it is clearly labeled. Deprecation
satisfies the requirement ("no consumer can read it as a live risk
measure") with much less collateral risk, and matches the WP-B2 authors'
explicit original intent (`objectives.py` comment: "still a mathematically
valid statistic; anything reading it directly keeps working").

**Fix — deprecated at every surface that exposes it:**
- `src/autoscreener/api/schemas.py`: field now declared with
  `Field(deprecated="...")` — this is a real Pydantic 2 `deprecated`
  marker, so it appears as `"deprecated": true` in the actual OpenAPI
  schema (verified: `ModelV5DistributionView.model_json_schema()
  ["properties"]["expected_shortfall_10pct_log"]` now includes
  `'deprecated': True`), not just a source comment. Confirmed this does
  **not** fire a warning during normal API serialization —
  `model_dump()`/`model_dump_json()` don't trigger Pydantic's
  deprecated-field warning (only direct dotted attribute access would),
  and grepped the codebase for any such access: none exists.
- `frontend/src/api/types.ts`: `@deprecated` JSDoc on the field.
- `frontend/src/v5Labels.ts`: label suffixed "(旧)" ("old/legacy") with a
  comment pointing at the required UI annotation.
- `frontend/src/components/V5TickerDetailSection.tsx` +
  new `frontend/src/components/V5DeprecatedMetricNote.tsx`: the ticker
  detail row for this field now carries an inline, always-visible
  annotation ("※非推奨・全銘柄同一定数") with a tooltip explaining the
  degeneracy and pointing at the replacement field — following the same
  "never hide it, annotate it" convention already used by
  `V5FailureFloorNote`/`V5UnavailableMetric` in this codebase.
- `src/autoscreener/scoring/v5/engine.py`: **did not remove or silence**
  the per-run diagnostic. Added an explicit, per-field, commented allowlist
  `_KNOWN_CONSTANT_DISTRIBUTION_FIELDS = {"expected_shortfall_10pct_log":
  "<reason>"}`. The field is still measured and still reported as constant
  every run — it now emits `distribution_constant_field_known:
  expected_shortfall_10pct_log=-0.657881455 (<reason>)` instead of the
  plain `distribution_constant_field:...` tag that means "unexplained, go
  investigate". Any *other* field that turns up constant (a genuine
  regression, e.g. a repeat of the `model_confidence` defect) still gets
  the plain, undifferentiated tag.

**The related `objective_constant_term` noise (same diagnostic code).**
The same run diagnostic also fired `objective_constant_term` on declared
policy parameters — `tail_lambda=0.35`, `target_moic=10.0`,
`assumed_recovery=0.01`, and the other `*_lambda` coefficients in
`risk_adjusted_compounding` — which are constant by design (read once from
`ObjectivesConfig` or a shared module constant, never derived from a
ticker's own data), not by defect. Added
`_POLICY_PARAMETER_EXPLANATION_KEYS`, an explicit per-objective allowlist
of declared-constant explanation keys (`ten_bagger.target_moic`,
`risk_adjusted.lambda`, and the five `risk_adjusted_compounding` lambda
coefficients plus `assumed_recovery`). Only keys on this list are excluded
from `_constant_explanation_terms` — any *computed* term that happens to
come out constant (the actual shape of the original RACR defect) still
raises `objective_constant_term` exactly as before.

**Verification performed.**
- `tests/unit/test_v5_diagnostic_allowlists.py` (5 new tests): declared
  policy keys never raise `objective_constant_term` even when constant
  across every ticker in the sample; a genuinely constant *computed* term
  in the same explanation dict still raises it; the exclusion is scoped
  per-objective-name (does not leak to an unrelated objective reusing a key
  name); `expected_shortfall_10pct_log` gets the `_known` tag with a
  `deprecated` reason string; an unrelated constant field
  (`model_confidence`) still gets the plain unexplained tag.
- Existing `tests/unit/test_v5_racr_wp_b2.py` diagnostic tests (which
  assert the plain `distribution_constant_field:model_confidence=...` tag
  format) still pass unchanged — the allowlist only changes behavior for
  the one explicitly listed field.
- `uv run python -c "..."` manual check of the generated OpenAPI schema
  (see above) confirming `deprecated: true` is actually present.
- Did not run `run-v5-shadow` (out of scope per the task's constraints) —
  the diagnostic behavior is verified via the pure unit-tested functions
  only, not a live run.

**Files:** `src/autoscreener/scoring/v5/engine.py`,
`src/autoscreener/api/schemas.py`, `frontend/src/api/types.ts`,
`frontend/src/v5Labels.ts`, `frontend/src/components/V5TickerDetailSection.tsx`,
`frontend/src/components/V5DeprecatedMetricNote.tsx` (new),
`frontend/src/index.css`,
`tests/unit/test_v5_diagnostic_allowlists.py` (new).

---

## Full verification summary

- Backend: `TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test uv run pytest -q`
  → **1204 passed, 0 failed** (baseline 1190 + 14 new tests: 7 alembic
  guard + 2 CORS/Vite alignment + 5 diagnostic allowlist).
- Frontend: `npm run build` → clean. `npm test -- --run` → 3 files / 6
  tests passed (baseline 4 + 2 new). `npm run lint` → 17 warnings / 0
  errors (unchanged from baseline).
- alembic: manually exercised all three invocation shapes against the real
  Postgres instance (dev DB read via `alembic current`, explicit
  `DATABASE_URL`-pointed test-DB invocation, and the exact 2026-09-05
  incident command) — see Defect 1 above.
- No writes were made to the dev `autoscreener` database; no Postgres
  roles/databases were created or dropped; `run-v5-shadow` was not run.

---

## Defect 4 — `forward_validation` reports all-zero indistinguishably from broken

**Hazard.** Stage 13 (`forward_validation`, `src/autoscreener/scoring/
forward_validation.py`) and its v5 analogue (stage 26,
`run_forward_validation_v5`) have run daily, reported `status=succeeded`,
and returned `{'computed': 0, 'not_matured': 0, 'missing_price': 0,
'settled_delisted': 0}` every day since scoring history began. This is the
promotion gate the whole model programme is blocked on
(`docs/investment_decision_gap_2026-09-04.md` / audit §13.4 gate #1), so an
operator staring at four zeros needs to know immediately whether the gate
is stuck because the model is broken or because not enough calendar time
has passed.

**Root cause (verified, not changed).** Both functions filter to rows old
enough for even the shortest horizon (1M = 30 days) *before* the
per-horizon loop:

```python
cutoff = as_of_date - datetime.timedelta(days=_MIN_HORIZON_DAYS)
scores = session.query(Score).filter(Score.score_date <= cutoff).all()
```

Confirmed against the real DB: the oldest `scores.score_date` is
2026-08-23; for `as_of=2026-09-04` the cutoff is 2026-08-05. No row
satisfies `score_date <= cutoff`, so the query returns zero rows and the
per-horizon loop (the only place that increments `not_matured`) never
runs. The four-zero result is not a bug in the returns computation — the
shortest horizon (1M) first matures around 2026-09-22 — but the *reporting*
made a healthy "nothing to do yet" state byte-for-byte identical to a
broken stage. `run_forward_validation_v5` has the exact same shape against
`ModelRun.as_of`/`ModelScore`.

**Fix — report the boundary, don't just compute past it.** No change to
`HORIZONS`, the cutoff arithmetic, the settlement logic, or how returns are
computed — this is purely additive to both functions' return dicts:

- `too_recent`: count of rows excluded by the cutoff (v4:
  `Score.score_date > cutoff`; v5: succeeded `ModelRun.as_of > cutoff`,
  scoped to `status == "succeeded"` so a failed/running run — which could
  never become eligible regardless of age — doesn't get counted as "close
  to the boundary").
- `cutoff_date`: the cutoff actually used, ISO date string.
- `oldest_score_date`: the oldest score/run date in the table (v5: among
  succeeded runs only), or `null` if the table is empty.
- `first_horizon_matures_on`: `oldest_score_date + 30 days` — the date the
  first forward return can possibly appear, or `null` if there is no data
  yet at all.

These are plain dict keys, so they flow unchanged through
`PipelineRecorder.stage()` into `pipeline_stage_runs.result` (a JSON
column) with no schema change.

**Frontend.** `frontend/src/pipelineStages.ts`: `forward_validation` (and
`forward_validation_v5`, previously unlabeled and left to the raw
`key: value` fallback) now route through a new
`formatForwardValidationResult`. When `computed > 0` it still shows just
the count (`算出N件`), unchanged from before. When `computed === 0` and
`too_recent > 0`, it appends the boundary in one line: `算出0件(未成熟N件・
最古スコア2026-08-23・成熟見込み2026-09-22〜)`. Any other zero-result shape
(e.g. all `missing_price`) falls through to the plain `算出0件` — this fix
targets specifically the cutoff-exclusion blind spot described above, not
every possible zero-result cause. Also added a `forward_validation_v5`
label ("前方検証(v5)") to `STAGE_LABELS`, which had none before (it fell
back to the raw stage name).

**Judgement call: `succeeded`, not a new status.** An all-zero-because-
too-recent run is not an error — nothing failed, nothing is misconfigured,
the pipeline did exactly what it should given the data it has. Inventing a
distinct non-failing status (e.g. `pending`/`insufficient_history`) would
mean teaching `monitoring.determine_run_status` and
`frontend/src/pipelineStages.ts`'s `CORE_STAGES` handling a fifth state
across the whole pipeline for one stage's one boundary condition, and
`forward_validation` is a `CORE_STAGE` — a status other than
`succeeded`/`failed` on a core stage risks being read as "the run is
degraded" by every existing consumer of `pipeline_runs.status`, which is
the exact false alarm this fix exists to prevent in the other direction.
The right layer for "is this expected right now" is the `result` payload,
which is what this fix adds — `status` answers "did the stage do what it
was supposed to", and it did.

**Verification performed.**
- `tests/unit/test_forward_validation.py`:
  `test_not_matured_before_shortest_horizon_is_skipped_entirely` extended
  to assert `computed == 0`, `cutoff_date` matches the exact expected
  value, `too_recent >= 1` (the fixture's own row is guaranteed to be
  excluded), and `first_horizon_matures_on == oldest_score_date + 30d`
  (self-consistency, since the shared test DB's global oldest-score-date
  isn't a fixed value to hardcode against).
  `test_matured_horizon_computes_realized_return` extended to assert
  `computed >= 1` still holds unchanged and the new keys are present with
  the right types alongside it.
- `tests/unit/test_v5_phase7_backtest_infrastructure.py`: new
  `test_forward_validation_v5_reports_too_recent_boundary` — inserts a
  succeeded `ModelRun` deliberately too recent for the 1M horizon and
  asserts the same four fields on the v5 path.
- `TEST_DATABASE_URL=... uv run pytest tests/ -q` → **1209 passed, 0
  failed** (baseline 1208 + 1 net new test function; two existing tests
  were extended in place rather than duplicated).
- Frontend: `npm run build` → clean. `npm test -- --run` → 3 files / 6
  tests passed, unchanged (no test file targets `pipelineStages.ts`
  directly). `npm run lint` → 17 warnings / 0 errors, unchanged.
- Did not run `run-v5-shadow` or the daily pipeline (forbidden by this
  task's constraints); did not write to the dev `autoscreener` database.

**Left undone / out of scope.** The underlying fact — scoring history is
9-12 days old against a 30-day minimum horizon — is not a bug and is not
fixed here; per `docs/model-v5-phase-progress.md` this is the one thing
still blocking promotion out of `CONTINUE_SHADOW`, and it resolves itself
by calendar time (first maturity ≈2026-09-22), not by code. This defect
was purely about making that fact visible without reading source.

**Files:** `src/autoscreener/scoring/forward_validation.py`,
`frontend/src/pipelineStages.ts`,
`tests/unit/test_forward_validation.py`,
`tests/unit/test_v5_phase7_backtest_infrastructure.py`.

---

## Left undone / not verified

- Did not re-run `run-v5-shadow` to confirm the diagnostic's live output
  format post-fix (`distribution_constant_field_known:...` actually
  appearing in a real `model_runs.metrics` row) — forbidden by this task's
  constraints. Confidence rests on the unit tests against the same pure
  functions `run_v5_shadow` calls.
- Did not audit every other doc/README mention of
  `expected_shortfall_10pct_log` beyond the API schema, frontend
  types/labels/UI, and the two existing WP-B/WP-B2 test files found by
  grep; none turned up elsewhere (checked `docs/*.md` glossary-style pages
  — no separate glossary entry exists for this field).
