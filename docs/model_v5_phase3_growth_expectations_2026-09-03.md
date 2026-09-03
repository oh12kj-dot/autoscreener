# Model v5 Phase 3 — Growth / TAM / KPI / Expectations (2026-09-03)

Source of truth: GitHub Issue #3, Phase 3.

## Operational boundary

Codex did not start, stop, or restart the daily batch. The Windows-scheduled run
started independently at 09:00 JST and was still in its collection stage during
the final Phase 3 verification. Real-model validation used only the dedicated
`run-v5-shadow --date 2026-09-02` command against the completed PIT universe.

## Implemented state updates

### Growth duration and TAM

TAM is never an additive positive score. A valid same-currency estimate requires
positive TAM and positive current addressable revenue. It produces a time-to-TAM
ceiling on growth duration. A large TAM leaves the prior duration unchanged;
high penetration can shorten the fade period. Low-confidence machine extraction
does not pass the feature reliability floor.

### Operating KPI nowcast

Only within-company, same-definition changes are used. ARR, backlog, RPO, GMV,
TPV, customers, and store count require a 60–450 day comparable interval.
Production, a flow metric, requires a near-year comparison. Non-positive values,
unmatched histories, and extreme annualised log changes are rejected. Accepted
changes make a bounded near-term growth observation update.

### Consensus revision

Consensus uses two snapshots for the same source, period type, and period end,
with both `observed_at < as_of + 1 day`. The change in the revenue estimate is
annualised to the forecast period, reliability-scaled by source confidence and
analyst count, then bounded before changing the initial growth state. Consensus
is an observation update, not ground truth.

### Management guidance

Guidance remains separate from consensus. Only forward-dated revenue/sales
guidance with positive, ordered USD bounds and a plausible midpoint-to-trailing
revenue ratio is accepted. Missing guidance is neutral. Inverted ranges and
likely unit/scale mismatches are explicitly rejected.

## Coverage-bias control and missingness

Each feature is enabled by config but also requires universe-wide data coverage.
If its coverage floor is not met, it is disabled for every ticker, including
tickers that happen to have a row. This prevents collection scope from becoming
a rank advantage. Missing/failed inputs never change the growth mean; confidence
is handled separately. Merely having an optional observation does not grant a
confidence bonus.

Every ticker stores all four feature states and all four ablation states.
Unavailable or coverage-gated features have `status=not_computed` plus a reason,
not a fabricated zero impact. Applied features store the full-versus-without
state shift, P(target) change, expected-CAGR change, and counterfactual output.

## Real-data result

Current-code shadow run:

- run ID: `ad0b5cbe-128f-4cbf-94f2-154029718884`
- as-of: `2026-09-02`
- implementation version: `v5.phase3`
- config hash: `9b02daeef49c8c93`
- population: 1,273
- PIT-ready inputs: 1,215
- available distributions: 1,165
- unavailable distributions: 108
- objective rows: 6,365

Observed universe coverage:

- consensus: 100.00% — runtime enabled
- operating KPI: 9.58% — globally coverage-gated
- guidance: 7.62% — globally coverage-gated
- TAM: 0.24% — globally coverage-gated

Consensus produced 81 valid revisions; 74 belonged to tickers with an available
base distribution and therefore changed a state. The other seven retained their
explicit unavailable distribution. Applied revisions had 74 computed ablations:
34 positive and 40 negative P(target) effects. Every ticker had an ablation entry
for every Phase 3 feature; 5,018 feature/ticker combinations were explicitly not
computed with their reason.

PIT and mathematical checks:

- feature evidence later than the as-of cutoff: 0
- probability-order violations: 0
- quantile-order violations: 0
- ES10/P10 violations: 0
- applied feature missing its ablation: 0
- ticker missing any Phase 3 ablation contract: 0

Live API smoke checks for latest run, objective-ranked list, and ticker detail
returned HTTP 200. The detail response exposed `v5.phase3` states and all four
feature ablation records.

The v4 production table was unchanged immediately before and after the dedicated
shadow run:

- rows: 8,225
- fingerprint: `cd3129d07f91dd3f66704ec759b7f2bc`

## Tests

- Phase 3 focused tests: 10 passed
- v5 Phase 1–3 focused tests: 24 passed
- v5 plus API regression selection: 78 passed
- complete backend suite: 883 passed
- frontend tests: 2 passed
- frontend lint: exit 0 with the same 15 existing React warnings
- frontend production build: PASS, 638 modules

## Phase 3 verdict

PASS for implementation and honest shadow validation. Consensus revisions are
the only Phase 3 signal allowed to affect the real distribution because the
other datasets do not meet their coverage floors. TAM/KPI/guidance transforms
are implemented and unit/integration tested, but their real-data contribution is
correctly `not_computed`; they are not accepted as production predictive signals.
Feature efficacy remains a Phase 7 forward/backtest question.
