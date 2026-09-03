"""Phase 7: v4-vs-v5 comparison infrastructure and v5 historical-run PIT enforcement.

**This module produces no promotion judgment and is not itself a backtest.**
Issue #3 section 31 explicitly prohibits comparing v5 against a broken
baseline. As measured directly against this database (2026-09-03; see
docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md for the full
evidence): ``raw_snapshots.available_from`` -- the point-in-time financial
statement history both v4 and v5 depend on -- spans only 2026-08-23 to
2026-09-03 (a ~9 trading-day window), against a 7-year target horizon
(2,557 days). v4's own backtest machinery (``backtest/runner.py:
_evaluate_one_date``) already discards any observation whose realized
return cannot be computed (``if realized is None: continue``), which is
exactly why v4's own effective evaluation window is a handful of days, not
a v5-specific gap. **Neither model has a realized-outcome-based historical
backtest available today** -- this is a shared, root-cause data-maturity
problem, not something either model's code can work around.

Given that, this module provides what IS possible and useful today:

1. ``historical_feature_flags()``/``run_v5_historical()``: force-disable any
   v5 feature whose ``FeatureSpec.historical_backtest_supported`` is False
   (Issue #3 section 25), regardless of config, with the forced-off set
   recorded in the run -- ready infrastructure for whenever real historical
   PIT depth exists, and directly testable today even though no historical
   run currently produces a materially different result (the affected
   features -- litigation/macro_regime/acquisition_competing_risk -- are
   already coverage-gated off at current real coverage levels).
2. ``compare_v4_v5_same_day()``: a same-day, read-only, cross-sectional
   comparison between v4's live ``scores`` and a v5 shadow run on the same
   ``as_of`` -- population overlap, rank correlation, sector/size
   distribution, v5 feature coverage. This is NOT a backtest (no realized
   return is read or required) and is explicitly labeled as such in its
   output (``not_a_backtest=True``, ``decision_input_only=True``) so it
   cannot be mistaken for backtest evidence by a later reader.
"""

from __future__ import annotations

import datetime
import statistics
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from autoscreener.backtest.metrics import spearman
from autoscreener.config import ModelV5Config, ObjectivesConfig, load_model_v5_config, load_objectives_config
from autoscreener.db.models import ModelRun, ModelScore, ObjectiveScore, Score
from autoscreener.scoring.moic import MoicInputs
from autoscreener.scoring.v5.engine import run_v5_shadow
from autoscreener.scoring.v5.feature_registry import FEATURES_BY_KEY

# A date-block bootstrap confidence interval needs enough independent
# evaluation dates to be meaningful; below this, the function refuses
# rather than reporting a fabricated-precision CI from a handful of dates
# (Issue #3 section 3.3's KPI-acceptance discipline, same spirit as
# ``KpiAcceptanceConfig.min_effective_dates`` for v4).
MIN_BOOTSTRAP_DATES = 30


def historical_feature_flags(model_config: ModelV5Config) -> tuple[dict[str, bool], tuple[str, ...]]:
    """Force-disable every feature the registry marks PIT-unsupported for
    historical reconstruction (Issue #3 section 25), regardless of what
    ``config/model_v5.yaml`` says. Returns ``(overridden_flags,
    forced_off_keys)`` -- the harness's enforcement wins over config, and
    the forced-off set is always returned so the caller can record it.
    """
    overridden = dict(model_config.feature_flags)
    forced_off: list[str] = []
    for key, spec in FEATURES_BY_KEY.items():
        if not spec.historical_backtest_supported:
            was_enabled = overridden.get(key, spec.default_enabled)
            overridden[key] = False
            if was_enabled:
                forced_off.append(key)
    return overridden, tuple(sorted(forced_off))


def run_v5_historical(
    as_of: datetime.date,
    *,
    model_config: ModelV5Config | None = None,
    objectives_config: ObjectivesConfig | None = None,
) -> dict:
    """Run v5 in "historical mode": PIT-unsupported features are force-
    disabled regardless of config (see ``historical_feature_flags``), and
    the forced-off set is recorded on the returned result so a reader never
    has to re-derive which features were suppressed for a given run.
    """
    model_config = model_config or load_model_v5_config()
    overridden_flags, forced_off = historical_feature_flags(model_config)
    historical_config = model_config.model_copy(update={"feature_flags": overridden_flags})
    result = run_v5_shadow(
        as_of, model_config=historical_config, objectives_config=objectives_config,
    )
    result["historical_mode"] = True
    result["forced_disabled_features"] = list(forced_off)
    return result


@dataclass(frozen=True)
class ComparisonRecord:
    """Same-day v4-vs-v5 cross-sectional comparison. Never a backtest, never
    a promotion input by itself -- both flags below are always True and are
    part of the persisted shape specifically so a downstream reader cannot
    mistake this for realized-outcome evidence."""

    evaluation_date: str
    v4_config_hash: str | None
    v5_run_id: str | None
    v5_config_hash: str | None
    code_revision: dict
    v4_population: int
    v5_population: int
    overlap_population: int
    v5_feature_coverage: dict
    rank_correlation_spearman: float | None
    sector_concentration_v4: dict[str, int]
    sector_concentration_v5: dict[str, int]
    size_distribution_v4: dict[str, float | None]
    size_distribution_v5: dict[str, float | None]
    warnings: list[str]
    not_a_backtest: bool = True
    decision_input_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _size_distribution(market_caps: list[float]) -> dict[str, float | None]:
    if not market_caps:
        return {"n": 0, "median": None, "p10": None, "p90": None}
    ordered = sorted(market_caps)
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "p10": ordered[max(0, int(len(ordered) * 0.10) - 1)],
        "p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.90))],
    }


def _sector_counts(sectors: list[str | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sector in sectors:
        key = sector or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def compare_v4_v5_same_day(
    session: Session, as_of: datetime.date, *, v5_run_id: str | None = None,
) -> ComparisonRecord:
    """Read-only same-day comparison. Reads v4's live ``scores`` (never
    writes to it) and the latest succeeded v5 ``model_runs`` row for
    ``as_of`` (or a specific ``v5_run_id`` if given). No realized return is
    read or required -- this compares two point-in-time distributions on
    the same evaluation date, not backtest outcomes.
    """
    warnings: list[str] = [
        "not_a_backtest: no realized return is read or required",
        "return_metrics_not_computable: raw_snapshots PIT history exists only "
        "for a ~9 trading-day window (2026-08-23..2026-09-03) against a 7-year "
        "target horizon; see docs/model_v5_phase7_backtest_infrastructure_2026-09-03.md",
    ]

    v4_rows = session.query(Score).filter(Score.score_date == as_of).all()
    v4_config_hash = v4_rows[0].config_hash if v4_rows else None
    v4_by_ticker: dict[int, Score] = {row.ticker_id: row for row in v4_rows}

    if v5_run_id is not None:
        v5_run = session.get(ModelRun, v5_run_id)
    else:
        v5_run = (
            session.query(ModelRun)
            .filter(ModelRun.as_of == as_of, ModelRun.status == "succeeded")
            .order_by(ModelRun.started_at.desc())
            .first()
        )
    if v5_run is None:
        warnings.append("no_succeeded_v5_run_found_for_date")
        return ComparisonRecord(
            evaluation_date=as_of.isoformat(), v4_config_hash=v4_config_hash,
            v5_run_id=None, v5_config_hash=None, code_revision={},
            v4_population=len(v4_rows), v5_population=0, overlap_population=0,
            v5_feature_coverage={}, rank_correlation_spearman=None,
            sector_concentration_v4=_sector_counts(
                [MoicInputs.from_dict(r.inputs).sector for r in v4_rows if r.inputs]
            ),
            sector_concentration_v5={}, size_distribution_v4=_size_distribution(
                [MoicInputs.from_dict(r.inputs).market_cap for r in v4_rows if r.inputs]
            ),
            size_distribution_v5={"n": 0, "median": None, "p10": None, "p90": None},
            warnings=warnings,
        )

    v5_ten_bagger = {
        row.ticker_id: float(row.score_value)
        for row in session.query(ObjectiveScore).filter_by(
            run_id=v5_run.id, objective="ten_bagger",
        ).all()
        if row.score_value is not None
    }
    # v5's ModelScore.states does not carry market_cap/sector directly (they
    # are v4 MoicInputs concepts, not part of the Phase 2 state contract);
    # read them from the same v4 Score.inputs blob for the same ticker when
    # available, since both models score the same underlying company on the
    # same day. A lightweight ticker_id-only query avoids pulling the full
    # (large) ModelScore.features/states JSONB payloads just for this.
    v5_ticker_ids = [
        row.ticker_id for row in session.query(ModelScore.ticker_id).filter_by(run_id=v5_run.id).all()
    ]
    v5_market_caps: list[float] = []
    v5_sectors: list[str | None] = []
    for ticker_id in v5_ticker_ids:
        v4_row = v4_by_ticker.get(ticker_id)
        if v4_row is not None and v4_row.inputs:
            inputs = MoicInputs.from_dict(v4_row.inputs)
            v5_market_caps.append(inputs.market_cap)
            v5_sectors.append(inputs.sector)

    overlap_ids = sorted(set(v4_by_ticker) & set(v5_ten_bagger))
    if len(overlap_ids) >= 3:
        v4_probs = [float(v4_by_ticker[t].probability) for t in overlap_ids if v4_by_ticker[t].probability is not None]
        paired = [
            (float(v4_by_ticker[t].probability), v5_ten_bagger[t])
            for t in overlap_ids if v4_by_ticker[t].probability is not None
        ]
        rank_corr = spearman([p[0] for p in paired], [p[1] for p in paired]) if len(paired) >= 3 else None
    else:
        rank_corr = None
        warnings.append("overlap_population_below_3_no_rank_correlation")

    return ComparisonRecord(
        evaluation_date=as_of.isoformat(),
        v4_config_hash=v4_config_hash,
        v5_run_id=str(v5_run.id),
        v5_config_hash=v5_run.config_hash,
        code_revision=(v5_run.metrics or {}).get("code_revision", {}),
        v4_population=len(v4_rows),
        v5_population=v5_run.population_count,
        overlap_population=len(overlap_ids),
        v5_feature_coverage=(v5_run.metrics or {}).get("feature_universe_coverage", {}),
        rank_correlation_spearman=rank_corr,
        sector_concentration_v4=_sector_counts(
            [MoicInputs.from_dict(r.inputs).sector for r in v4_rows if r.inputs]
        ),
        sector_concentration_v5=_sector_counts(v5_sectors),
        size_distribution_v4=_size_distribution(
            [MoicInputs.from_dict(r.inputs).market_cap for r in v4_rows if r.inputs]
        ),
        size_distribution_v5=_size_distribution(v5_market_caps),
        warnings=warnings,
    )


def date_block_bootstrap_ci(
    per_date_values: dict[str, float], *, iterations: int = 2000, min_dates: int = MIN_BOOTSTRAP_DATES,
) -> dict:
    """Date-block bootstrap confidence interval over one aggregate value per
    evaluation date (resampling whole dates, not individual tickers, so
    within-date correlation is respected -- the standard backtest-CI
    pattern). Refuses to produce a CI below ``min_dates`` distinct dates
    rather than reporting a fabricated-precision interval from too few
    blocks; the current real evaluation-date count for v5 (9, all of Phase
    0's 2026-08-23..2026-09-02 window) is well below this floor -- see the
    Phase 7 doc for why deeper regime stratification/bias auditing is
    deferred rather than attempted at that sample size.
    """
    dates = sorted(per_date_values)
    if len(dates) < min_dates:
        return {
            "status": "insufficient_dates", "available_dates": len(dates),
            "required_dates": min_dates, "mean": None, "ci_low": None, "ci_high": None,
        }
    import random

    values = [per_date_values[d] for d in dates]
    rng = random.Random(1234567)
    means = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(statistics.mean(sample))
    means.sort()
    lo_idx = max(0, int(0.025 * len(means)))
    hi_idx = min(len(means) - 1, int(0.975 * len(means)))
    return {
        "status": "computed", "available_dates": len(dates), "required_dates": min_dates,
        "mean": statistics.mean(values), "ci_low": means[lo_idx], "ci_high": means[hi_idx],
        "iterations": iterations,
    }
