"""Cross-sectional coverage-bias audit for the incumbent v4 ranking.

This does not claim causality: v4 does not read Live Intelligence tables.  It
measures whether better-covered companies nevertheless dominate the current
ranking, which would make a later v5 feature comparison vulnerable to coverage
selection bias.
"""

from __future__ import annotations

import datetime
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.backtest.metrics import spearman
from autoscreener.coverage import SUCCESSFUL_COVERAGE_STATUSES, CoverageStatus
from autoscreener.db.models import LiveDatasetCoverage, Score


def audit_v4_coverage_bias(session: Session, *, score_date: datetime.date | None = None) -> dict:
    score_date = score_date or session.query(func.max(Score.score_date)).filter_by(scoring_version="v4").scalar()
    if score_date is None:
        return {"status": "INSUFFICIENT_DATA", "reason": "no_v4_scores", "score_date": None}

    scores = session.query(Score).filter(
        Score.scoring_version == "v4", Score.score_date == score_date, Score.probability.isnot(None),
    ).all()
    if len(scores) < 10:
        return {
            "status": "INSUFFICIENT_DATA", "reason": "fewer_than_10_v4_scores",
            "score_date": score_date.isoformat(), "population": len(scores),
        }

    latest: dict[tuple[int, str], LiveDatasetCoverage] = {}
    for row in session.query(LiveDatasetCoverage).filter(
        LiveDatasetCoverage.observed_at < datetime.datetime.combine(
            score_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc,
        )
    ).all():
        key = (row.ticker_id, row.dataset)
        current = latest.get(key)
        if current is None or (row.observed_at, row.id) > (current.observed_at, current.id):
            latest[key] = row

    by_ticker: dict[int, dict[str, str]] = defaultdict(dict)
    datasets: set[str] = set()
    for (ticker_id, dataset), row in latest.items():
        by_ticker[ticker_id][dataset] = row.coverage_status
        datasets.add(dataset)

    ordered = sorted(scores, key=lambda row: float(row.probability), reverse=True)
    top_count = max(1, len(ordered) // 10)
    top_ids = {row.ticker_id for row in ordered[:top_count]}
    probabilities = [float(row.probability) for row in ordered]
    successful_counts = [
        sum(status in SUCCESSFUL_COVERAGE_STATUSES for status in by_ticker[row.ticker_id].values())
        for row in ordered
    ]
    with_data_counts = [
        sum(status == CoverageStatus.COLLECTED_WITH_DATA for status in by_ticker[row.ticker_id].values())
        for row in ordered
    ]
    mean_all = sum(with_data_counts) / len(with_data_counts)
    mean_top = sum(with_data_counts[:top_count]) / top_count

    dataset_rates = {}
    for dataset in sorted(datasets):
        all_rate = sum(by_ticker[row.ticker_id].get(dataset) == CoverageStatus.COLLECTED_WITH_DATA for row in ordered) / len(ordered)
        top_rate = sum(by_ticker[ticker_id].get(dataset) == CoverageStatus.COLLECTED_WITH_DATA for ticker_id in top_ids) / top_count
        dataset_rates[dataset] = {
            "population_with_data_rate": all_rate,
            "top_decile_with_data_rate": top_rate,
            "difference": top_rate - all_rate,
        }

    probability_spearman = spearman(probabilities, [float(value) for value in with_data_counts])
    successful_spearman = spearman(probabilities, [float(value) for value in successful_counts])
    largest_dataset_gap = max((abs(item["difference"]) for item in dataset_rates.values()), default=0.0)
    review_required = abs(probability_spearman) > 0.10 or abs(mean_top - mean_all) > 1.0 or largest_dataset_gap > 0.15
    return {
        "status": "REVIEW_REQUIRED" if review_required else "NO_MATERIAL_ASSOCIATION",
        "score_date": score_date.isoformat(),
        "scoring_version": "v4",
        "population": len(ordered),
        "top_decile_count": top_count,
        "probability_vs_with_data_count_spearman": probability_spearman,
        "probability_vs_successful_coverage_count_spearman": successful_spearman,
        "mean_with_data_count": mean_all,
        "top_decile_mean_with_data_count": mean_top,
        "top_decile_coverage_advantage": mean_top - mean_all,
        "largest_absolute_dataset_rate_gap": largest_dataset_gap,
        "thresholds": {"absolute_spearman": 0.10, "dataset_count_advantage": 1.0, "dataset_rate_gap": 0.15},
        "dataset_rates": dataset_rates,
        "interpretation": (
            "Observational only: v4 does not consume Live Intelligence. A REVIEW_REQUIRED result "
            "means v5 validation must stratify or reweight by collection coverage."
        ),
    }
