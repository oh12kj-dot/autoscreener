"""上場廃止イベントの原因分類(docs/delisting_label_backfill_2026-09-04.md)。

純粋関数(`classify_from_filings`)のテストと、DB連携(`gather_evidence_for_event`
/ `classify_stored_delisting_events` / `apply_classifications`)のテストに分ける。
DB側は ZZ*** シンボルで後片付けする(既存の `test_delisting_source.py` と同じ作法)。
"""

from __future__ import annotations

import datetime

from autoscreener.batch.collect_delistings import classify_delistings
from autoscreener.collectors.delisting_classification import (
    EVENT_TYPES,
    FilingEvidence,
    apply_classifications,
    classify_from_filings,
    classify_stored_delisting_events,
    gather_evidence_for_event,
)
from autoscreener.db.models import DelistingEvent, Filing, Ticker
from autoscreener.db.session import session_scope


def _evidence(form: str, items: tuple[str, ...] = (), filed: datetime.date | None = None) -> FilingEvidence:
    return FilingEvidence(
        form=form,
        filed_date=filed or datetime.date(2024, 1, 1),
        items=items,
        document_url=f"https://www.sec.gov/example/{form}",
        accession_number="0000000000-24-000001",
    )


# --- classify_from_filings: 純粋ロジック ---------------------------------


def test_no_evidence_stays_unknown():
    result = classify_from_filings([])
    assert result.event_type == "unknown"
    assert result.confidence == "unknown"
    assert result.evidence_url is None


def test_bankruptcy_item_corroborated_by_deregistration_is_high_confidence():
    evidence = [
        _evidence("8-K", items=("1.03",), filed=datetime.date(2024, 3, 1)),
        _evidence("15-12B", filed=datetime.date(2024, 4, 1)),
    ]
    result = classify_from_filings(evidence)
    assert result.event_type == "bankruptcy"
    assert result.confidence == "high"
    assert result.evidence_form == "8-K"


def test_bankruptcy_item_without_deregistration_is_medium_confidence():
    evidence = [_evidence("8-K", items=("1.03",))]
    result = classify_from_filings(evidence)
    assert result.event_type == "bankruptcy"
    assert result.confidence == "medium"


def test_going_private_schedule_13e3_is_not_auto_classified_without_settlement():
    """Schedule 13E-3 is strong going-private evidence, but without a settlement
    value we must not write cash_acquisition/stock_acquisition — backtest/runner.py
    reads an unsettled acquisition-type label as a full -100% loss, which a
    going-private buyout usually is not. Evidence is still surfaced for a human."""
    evidence = [_evidence("SC 13E3"), _evidence("25")]
    result = classify_from_filings(evidence)
    assert result.event_type == "unknown"
    assert result.evidence_form == "SC 13E3"
    assert "settlement value" in result.rationale


def test_acquisition_completion_alone_stays_unknown():
    # Item 2.01 alone (an ordinary asset disposal) must NOT be read as the
    # registrant's own delisting cause.
    evidence = [_evidence("8-K", items=("2.01",))]
    result = classify_from_filings(evidence)
    assert result.event_type == "unknown", "2.01 alone is not enough evidence of the registrant's own delisting"


def test_acquisition_completion_with_deregistration_still_stays_unknown_without_settlement():
    """Even corroborated by an actual deregistration filing, Item 2.01 does not
    say cash vs. stock or the per-share amount — writing cash_acquisition /
    stock_acquisition here would be read downstream as a full loss (see
    backtest/runner.py:479-484), which is worse than leaving it unknown."""
    evidence = [
        _evidence("8-K", items=("2.01",), filed=datetime.date(2024, 5, 1)),
        _evidence("25", filed=datetime.date(2024, 5, 15)),
    ]
    result = classify_from_filings(evidence)
    assert result.event_type == "unknown"
    assert result.evidence_form == "8-K"
    assert "cash or stock" in result.rationale


def test_delisting_notice_with_deregistration_stays_unknown_not_exchange_transfer():
    """exchange_transfer is treated identically to unknown by backtest/runner.py
    (-100%), so asserting it from a mere deficiency notice adds false certainty
    with no numeric benefit — leave it unknown but keep the evidence visible."""
    evidence = [
        _evidence("8-K", items=("3.01",), filed=datetime.date(2024, 2, 1)),
        _evidence("25-NSE", filed=datetime.date(2024, 2, 20)),
    ]
    result = classify_from_filings(evidence)
    assert result.event_type == "unknown"
    assert result.evidence_form == "8-K"
    assert "exchange_transfer" in result.rationale


def test_deregistration_form_alone_does_not_imply_a_cause():
    """Form 15 alone follows bankruptcy, going-private, AND voluntary exits alike —
    asserting any specific cause from it alone would be a guess."""
    evidence = [_evidence("15-12G")]
    result = classify_from_filings(evidence)
    assert result.event_type == "unknown"
    assert result.evidence_form == "15-12G"  # evidence is recorded even though unclassified
    assert "does not by itself state a cause" in result.rationale


def test_bankruptcy_takes_precedence_over_delisting_notice():
    evidence = [
        _evidence("8-K", items=("1.03",), filed=datetime.date(2024, 1, 1)),
        _evidence("8-K", items=("3.01",), filed=datetime.date(2024, 1, 5)),
        _evidence("25", filed=datetime.date(2024, 2, 1)),
    ]
    result = classify_from_filings(evidence)
    assert result.event_type == "bankruptcy"


def test_event_types_match_the_taxonomy_already_consumed_by_runner_and_routes():
    """Must match alembic/versions/b3f6d1a08c92_delisting_event_type_check.py AND
    the values backtest/runner.py:472-489 / api/routes.py's mna-history endpoint
    already branch on — introducing a new value here would be silently ignored
    (or misread as a full loss) by that existing consumption logic."""
    assert "unknown" in EVENT_TYPES
    assert set(EVENT_TYPES) == {
        "unknown",
        "cash_acquisition",
        "stock_acquisition",
        "bankruptcy",
        "liquidation",
        "exchange_transfer",
    }


# --- DB連携:gather_evidence_for_event / classify_stored_delisting_events -----


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()
        ids = [t.id for t in tickers]
        if ids:
            session.query(DelistingEvent).filter(DelistingEvent.ticker_id.in_(ids)).delete(
                synchronize_session=False
            )
            session.query(Filing).filter(Filing.ticker_id.in_(ids)).delete(synchronize_session=False)
        for t in tickers:
            session.delete(t)


def test_gather_evidence_uses_ticker_id_not_cik_and_flags_shared_cik():
    """回帰テスト:2026-09-04調査で見つかった実際の誤帰属パターン
    (CIK 0000098222 を現役銘柄 TDW と廃止銘柄 TDGMW が共有)を再現する。
    `cik` でJOINすると無関係な現役銘柄の書類を証拠にしてしまうため、
    `ticker_id` だけで引き、共有CIKがあれば `ambiguous_shared_cik=True` にする。
    """
    active_symbol, delisted_symbol = "ZZDCSHARE_ACT", "ZZDCSHARE_DEL"
    shared_cik = "0000900099"
    _cleanup([active_symbol, delisted_symbol])
    event_date = datetime.date(2024, 6, 1)
    try:
        with session_scope() as session:
            active = Ticker(symbol=active_symbol, market="US", cik=shared_cik)
            delisted = Ticker(
                symbol=delisted_symbol, market="US", cik=shared_cik,
                delisted_at=datetime.datetime.combine(event_date, datetime.time(), tzinfo=datetime.timezone.utc),
            )
            session.add_all([active, delisted])
            session.flush()
            # Filing belongs to the ACTIVE ticker only (its own ticker_id) — must
            # never leak into the delisted ticker's evidence via a cik-based join.
            session.add(Filing(
                ticker_id=active.id, cik=shared_cik, accession_number="0000900099-24-000001",
                form="8-K", filed_date=event_date, items=["1.03"],
                document_url="https://www.sec.gov/example/active-8k",
            ))
            event = DelistingEvent(
                ticker_id=delisted.id, event_date=event_date, event_type="unknown",
                source="ticker_master_backfill", observed_at=datetime.datetime.now(datetime.timezone.utc),
                confidence="low",
            )
            session.add(event)
            session.flush()

            bundle = gather_evidence_for_event(session, event, delisted)
            assert bundle.evidence == (), "the active ticker's filing must not appear as the delisted ticker's evidence"
            assert bundle.ambiguous_shared_cik is True
            assert bundle.shared_cik_active_symbols == (active_symbol,)
    finally:
        _cleanup([active_symbol, delisted_symbol])


def test_classify_stored_delisting_events_end_to_end_with_apply():
    symbol = "ZZDCBANKRUPT"
    _cleanup([symbol])
    event_date = datetime.date(2024, 3, 10)
    try:
        with session_scope() as session:
            ticker = Ticker(
                symbol=symbol, market="US", cik="0000900100",
                delisted_at=datetime.datetime.combine(event_date, datetime.time(), tzinfo=datetime.timezone.utc),
            )
            session.add(ticker)
            session.flush()
            session.add_all([
                Filing(
                    ticker_id=ticker.id, cik="0000900100", accession_number="0000900100-24-000001",
                    form="8-K", filed_date=event_date - datetime.timedelta(days=10), items=["1.03"],
                    document_url="https://www.sec.gov/example/bankrupt-8k",
                ),
                Filing(
                    ticker_id=ticker.id, cik="0000900100", accession_number="0000900100-24-000002",
                    form="15-12B", filed_date=event_date, items=[],
                    document_url="https://www.sec.gov/example/bankrupt-15",
                ),
            ])
            session.add(DelistingEvent(
                ticker_id=ticker.id, event_date=event_date, event_type="unknown",
                source="ticker_master_backfill", observed_at=datetime.datetime.now(datetime.timezone.utc),
                confidence="low",
            ))

        with session_scope() as session:
            outcomes = classify_stored_delisting_events(session)
            mine = [o for o in outcomes if o.bundle.symbol == symbol]
            assert len(mine) == 1
            assert mine[0].classification.event_type == "bankruptcy"
            assert mine[0].classification.confidence == "high"

            counts = apply_classifications(session, mine)
            assert counts == {"classified": 1, "left_unknown": 0, "ambiguous_shared_cik_skipped": 0}

        with session_scope() as session:
            row = session.query(DelistingEvent).filter_by(ticker_id=ticker.id).one()
            assert row.event_type == "bankruptcy"
            assert row.confidence == "high"
            assert row.source_url == "https://www.sec.gov/example/bankrupt-8k"
            # Provenance of the event's existence is untouched.
            assert row.source == "ticker_master_backfill"
    finally:
        _cleanup([symbol])


def test_classify_delistings_dry_run_does_not_write():
    symbol = "ZZDCDRYRUN"
    _cleanup([symbol])
    event_date = datetime.date(2024, 3, 10)
    try:
        with session_scope() as session:
            ticker = Ticker(
                symbol=symbol, market="US", cik="0000900101",
                delisted_at=datetime.datetime.combine(event_date, datetime.time(), tzinfo=datetime.timezone.utc),
            )
            session.add(ticker)
            session.flush()
            session.add(Filing(
                ticker_id=ticker.id, cik="0000900101", accession_number="0000900101-24-000001",
                form="8-K", filed_date=event_date - datetime.timedelta(days=5), items=["1.03"],
                document_url="https://www.sec.gov/example/dryrun-8k",
            ))
            session.add(DelistingEvent(
                ticker_id=ticker.id, event_date=event_date, event_type="unknown",
                source="ticker_master_backfill", observed_at=datetime.datetime.now(datetime.timezone.utc),
                confidence="low",
            ))

        result = classify_delistings(apply=False)
        outcomes = result.pop("outcomes")
        assert any(o.bundle.symbol == symbol and o.classification.event_type == "bankruptcy" for o in outcomes)

        with session_scope() as session:
            row = session.query(DelistingEvent).filter_by(ticker_id=ticker.id).one()
            assert row.event_type == "unknown", "dry-run must never write"
    finally:
        _cleanup([symbol])


def test_ambiguous_shared_cik_is_not_auto_applied():
    active_symbol, delisted_symbol = "ZZDCSHARE2_ACT", "ZZDCSHARE2_DEL"
    shared_cik = "0000900102"
    _cleanup([active_symbol, delisted_symbol])
    event_date = datetime.date(2024, 7, 1)
    try:
        with session_scope() as session:
            active = Ticker(symbol=active_symbol, market="US", cik=shared_cik)
            delisted = Ticker(
                symbol=delisted_symbol, market="US", cik=shared_cik,
                delisted_at=datetime.datetime.combine(event_date, datetime.time(), tzinfo=datetime.timezone.utc),
            )
            session.add_all([active, delisted])
            session.flush()
            # Evidence correctly attributed to the DELISTED ticker's own ticker_id
            # (not leaked from the active one) — but the shared CIK still warrants
            # a human check before writing.
            session.add(Filing(
                ticker_id=delisted.id, cik=shared_cik, accession_number="0000900102-24-000001",
                form="8-K", filed_date=event_date - datetime.timedelta(days=5), items=["1.03"],
                document_url="https://www.sec.gov/example/shared-8k",
            ))
            event = DelistingEvent(
                ticker_id=delisted.id, event_date=event_date, event_type="unknown",
                source="ticker_master_backfill", observed_at=datetime.datetime.now(datetime.timezone.utc),
                confidence="low",
            )
            session.add(event)

        with session_scope() as session:
            outcomes = classify_stored_delisting_events(session)
            mine = [o for o in outcomes if o.bundle.symbol == delisted_symbol]
            assert len(mine) == 1
            assert mine[0].classification.event_type == "bankruptcy"
            assert mine[0].bundle.ambiguous_shared_cik is True

            counts = apply_classifications(session, mine)
            assert counts == {"classified": 0, "left_unknown": 0, "ambiguous_shared_cik_skipped": 1}

        with session_scope() as session:
            row = session.query(DelistingEvent).filter_by(ticker_id=delisted.id).one()
            assert row.event_type == "unknown", "ambiguous shared-CIK evidence must not auto-apply"
    finally:
        _cleanup([active_symbol, delisted_symbol])
