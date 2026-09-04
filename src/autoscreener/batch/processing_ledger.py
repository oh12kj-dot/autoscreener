"""Versioned source-processing ledger used by incremental SEC extractors."""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from autoscreener.db.models import SourceProcessingLedger


def processed_source_keys(
    session: Session,
    *,
    source_type: str,
    processor: str,
    processor_version: str,
) -> set[str]:
    return {
        row[0]
        for row in session.query(SourceProcessingLedger.source_key)
        .filter_by(
            source_type=source_type,
            processor=processor,
            processor_version=processor_version,
        )
        .filter(SourceProcessingLedger.status.in_(("succeeded", "no_finding")))
        .all()
    }


def is_processed(
    session: Session,
    *,
    source_type: str,
    source_key: str,
    processor: str,
    processor_version: str,
) -> bool:
    return (
        session.query(SourceProcessingLedger.id)
        .filter_by(
            source_type=source_type,
            source_key=source_key,
            processor=processor,
            processor_version=processor_version,
        )
        .filter(SourceProcessingLedger.status.in_(("succeeded", "no_finding")))
        .first()
        is not None
    )


def record_processing(
    session: Session,
    *,
    ticker_id: int | None,
    source_type: str,
    source_key: str,
    processor: str,
    processor_version: str,
    status: str,
    detail: dict | None = None,
) -> None:
    row = (
        session.query(SourceProcessingLedger)
        .filter_by(
            source_type=source_type,
            source_key=source_key,
            processor=processor,
            processor_version=processor_version,
        )
        .one_or_none()
    )
    now = datetime.datetime.now(datetime.UTC)
    if row is None:
        session.add(
            SourceProcessingLedger(
                ticker_id=ticker_id,
                source_type=source_type,
                source_key=source_key,
                processor=processor,
                processor_version=processor_version,
                status=status,
                attempted_at=now,
                detail=detail,
            )
        )
        return
    row.ticker_id = ticker_id
    row.status = status
    row.attempted_at = now
    row.detail = detail
