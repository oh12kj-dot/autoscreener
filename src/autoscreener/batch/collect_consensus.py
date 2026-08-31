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
                inserted = unchanged = no_finding = 0
                # One provider/ticker failure must not poison the transaction
                # for every remaining ticker.  Exiting the savepoint also
                # flushes here, so DB constraint failures are caught locally.
                with session.begin_nested():
                    snapshots = provider.fetch(ticker.symbol, as_of)
                    if not snapshots:
                        no_finding = 1
                        payload = {"coverage_status": "collected_no_finding"}
                        digest = _hash(payload)
                        latest = session.query(AnalystConsensusSnapshot).filter_by(
                            ticker_id=ticker.id, source=provider.name
                        ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                        if latest and latest.content_hash == digest:
                            unchanged += 1
                        else:
                            session.add(AnalystConsensusSnapshot(
                                ticker_id=ticker.id, observed_at=as_of, source=provider.name,
                                period_type="NA", period_end=None, raw_payload=payload,
                                coverage_status="collected_no_finding", confidence="medium", content_hash=digest,
                            ))
                            inserted += 1
                    else:
                        seen: dict[tuple[str, datetime.date | None], str] = {}
                        for snap in snapshots:
                            payload = {k: v for k, v in vars(snap).items() if k != "observed_at"}
                            digest = _hash(payload)
                            key = (snap.source, snap.period_end)
                            if key in seen:
                                if seen[key] == digest:
                                    unchanged += 1
                                    continue
                                raise ValueError(
                                    "provider returned conflicting consensus rows "
                                    f"for source={snap.source} period_end={snap.period_end}"
                                )
                            seen[key] = digest
                            latest = session.query(AnalystConsensusSnapshot).filter_by(
                                ticker_id=ticker.id, source=snap.source, period_end=snap.period_end
                            ).order_by(AnalystConsensusSnapshot.observed_at.desc()).first()
                            if latest and latest.content_hash == digest:
                                unchanged += 1
                                continue
                            session.add(AnalystConsensusSnapshot(
                                ticker_id=ticker.id, observed_at=snap.observed_at, source=snap.source,
                                source_url=snap.source_url, period_type=snap.period_type, period_end=snap.period_end,
                                revenue_mean=snap.revenue_mean, revenue_low=snap.revenue_low,
                                revenue_high=snap.revenue_high, eps_mean=snap.eps_mean,
                                ebitda_mean=snap.ebitda_mean, analyst_count=snap.analyst_count,
                                target_price_mean=snap.target_price_mean, raw_payload=snap.raw_payload,
                                coverage_status="collected_with_data", confidence="medium", content_hash=digest,
                            ))
                            inserted += 1
                stats["inserted"] += inserted
                stats["unchanged"] += unchanged
                stats["no_finding"] += no_finding
            except Exception as exc:
                stats["failed"] += 1
                payload = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
                digest = _hash(payload)
                with session.begin_nested():
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
