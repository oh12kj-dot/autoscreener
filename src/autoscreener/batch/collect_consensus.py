"""Append-only, content-deduplicated consensus collection."""

from __future__ import annotations

import datetime
import hashlib
import json

from autoscreener.collectors.consensus import ConsensusProvider, YfinanceConsensusProvider
from autoscreener.db.models import AnalystConsensusSnapshot, Ticker
from autoscreener.db.session import session_scope


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def collect_consensus(provider: ConsensusProvider | None = None, *, as_of: datetime.datetime | None = None,
                      symbols: list[str] | None = None) -> dict[str, int]:
    provider = provider or YfinanceConsensusProvider()
    as_of = as_of or datetime.datetime.now(datetime.timezone.utc)
    stats = {"processed": 0, "inserted": 0, "unchanged": 0, "failed": 0, "no_finding": 0}
    with session_scope() as session:
        query = session.query(Ticker).filter(Ticker.is_benchmark.is_(False))
        if symbols:
            query = query.filter(Ticker.symbol.in_([s.upper() for s in symbols]))
        for ticker in query.all():
            stats["processed"] += 1
            try:
                snapshots = provider.fetch(ticker.symbol, as_of)
                if not snapshots:
                    snapshots = []
                    stats["no_finding"] += 1
                    payload = {"coverage_status": "collected_no_finding"}
                    digest = _hash(payload)
                    latest = session.query(AnalystConsensusSnapshot).filter_by(
                        ticker_id=ticker.id, source=provider.name
                    ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                    if latest and latest.content_hash == digest:
                        stats["unchanged"] += 1
                        continue
                    session.add(AnalystConsensusSnapshot(
                        ticker_id=ticker.id, observed_at=as_of, source=provider.name,
                        period_type="NA", period_end=None, raw_payload=payload,
                        coverage_status="collected_no_finding", confidence="medium", content_hash=digest,
                    ))
                    stats["inserted"] += 1
                    continue
                for snap in snapshots:
                    payload = {k: v for k, v in vars(snap).items() if k != "observed_at"}
                    digest = _hash(payload)
                    latest = session.query(AnalystConsensusSnapshot).filter_by(
                        ticker_id=ticker.id, source=snap.source, period_end=snap.period_end
                    ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                    if latest and latest.content_hash == digest:
                        stats["unchanged"] += 1
                        continue
                    session.add(AnalystConsensusSnapshot(
                        ticker_id=ticker.id, observed_at=snap.observed_at, source=snap.source,
                        source_url=snap.source_url, period_type=snap.period_type, period_end=snap.period_end,
                        revenue_mean=snap.revenue_mean, revenue_low=snap.revenue_low, revenue_high=snap.revenue_high,
                        eps_mean=snap.eps_mean, ebitda_mean=snap.ebitda_mean, analyst_count=snap.analyst_count,
                        target_price_mean=snap.target_price_mean, raw_payload=snap.raw_payload,
                        coverage_status="collected_with_data", confidence="medium", content_hash=digest,
                    ))
                    stats["inserted"] += 1
            except Exception as exc:
                stats["failed"] += 1
                payload = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
                digest = _hash(payload)
                latest = session.query(AnalystConsensusSnapshot).filter_by(
                    ticker_id=ticker.id, source=provider.name
                ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                if latest is None or latest.content_hash != digest:
                    session.add(AnalystConsensusSnapshot(
                        ticker_id=ticker.id, observed_at=as_of, source=provider.name,
                        period_type="NA", period_end=None, raw_payload=payload,
                        coverage_status="collection_failed", confidence="low", content_hash=digest,
                    ))
    return stats
