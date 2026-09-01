"""The single contract for Live Investment Intelligence collection coverage.

Coverage is evidence about a collection attempt, not a substitute value for a
missing financial observation.  Keeping it here prevents API, collectors and
the coverage dashboard from silently inventing different meanings for an empty
dataset.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from sqlalchemy.orm import Session


class CoverageStatus(StrEnum):
    NOT_COLLECTED = "not_collected"
    COLLECTED_NO_FINDING = "collected_no_finding"
    COLLECTED_WITH_DATA = "collected_with_data"
    COLLECTION_FAILED = "collection_failed"
    NOT_APPLICABLE = "not_applicable"


class CoverageReasonCode(StrEnum):
    OUTSIDE_COLLECTION_SCOPE = "outside_collection_scope"
    NO_RAW_SNAPSHOT = "no_raw_snapshot"
    INSUFFICIENT_ANNUAL_HISTORY = "insufficient_annual_history"
    NO_SUPPORTED_FILING = "no_supported_filing"
    SOURCE_NOT_SCANNED = "source_not_scanned"
    SOURCE_SCANNED_NO_MATCH = "source_scanned_no_match"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    USER_INPUT_MISSING = "user_input_missing"
    INSUFFICIENT_PRICE_HISTORY = "insufficient_price_history"
    INSUFFICIENT_FACTOR_HISTORY = "insufficient_factor_history"
    PROVIDER_ERROR = "provider_error"
    PARSE_ERROR = "parse_error"
    DATABASE_ERROR = "database_error"
    UNSUPPORTED_MODEL_FAMILY = "unsupported_model_family"


SUCCESSFUL_COVERAGE_STATUSES = frozenset({
    CoverageStatus.COLLECTED_NO_FINDING,
    CoverageStatus.COLLECTED_WITH_DATA,
})


def latest_dataset_coverage(
    session: Session, ticker_id: int, dataset: str, as_of: datetime.date,
):
    """Return the newest ledger row known at ``as_of`` (never future data)."""
    # Delayed import avoids a model -> coverage -> model import cycle.
    from autoscreener.db.models import LiveDatasetCoverage

    cutoff = datetime.datetime.combine(
        as_of + datetime.timedelta(days=1), datetime.time.min,
        tzinfo=datetime.timezone.utc,
    )
    return (
        session.query(LiveDatasetCoverage)
        .filter(
            LiveDatasetCoverage.ticker_id == ticker_id,
            LiveDatasetCoverage.dataset == dataset,
            LiveDatasetCoverage.observed_at < cutoff,
        )
        .order_by(LiveDatasetCoverage.observed_at.desc(), LiveDatasetCoverage.id.desc())
        .first()
    )
