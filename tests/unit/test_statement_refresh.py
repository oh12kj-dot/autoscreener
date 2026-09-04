import datetime

from sqlalchemy import event

from autoscreener.batch.statement_refresh import active_symbols_missing_statement_observation
from autoscreener.collectors.snapshot_collector import _STATEMENTS_AS_OF_KEY
from autoscreener.db.models import RawSnapshot, Ticker
from autoscreener.db.session import get_engine, session_scope


_SYMBOLS = ("ZZSTCURR", "ZZSTMISS", "ZZSTINEL", "ZZSTBENCH")


def _cleanup() -> None:
    with session_scope() as session:
        ids = [row[0] for row in session.query(Ticker.id).filter(Ticker.symbol.in_(_SYMBOLS)).all()]
        if ids:
            session.query(RawSnapshot).filter(RawSnapshot.ticker_id.in_(ids)).delete(
                synchronize_session=False
            )
            session.query(Ticker).filter(Ticker.id.in_(ids)).delete(synchronize_session=False)


def _raw(ticker_id: int, marker: str | None, tag: str) -> RawSnapshot:
    payload = {"info": {"marketCap": 1}, "balance_sheet": {}}
    if marker is not None:
        payload[_STATEMENTS_AS_OF_KEY] = marker
    return RawSnapshot(
        ticker_id=ticker_id,
        snapshot_date=datetime.date(2026, 9, 8),
        source="yfinance",
        payload=payload,
        content_hash=f"statement-refresh-{tag}",
        last_seen_date=datetime.date(2026, 9, 8),
        available_from=datetime.date(2026, 9, 8),
    )


def test_missing_statement_query_is_batched_and_respects_eligibility() -> None:
    _cleanup()
    try:
        with session_scope() as session:
            current = Ticker(symbol=_SYMBOLS[0], market="US", is_benchmark=False)
            missing = Ticker(symbol=_SYMBOLS[1], market="US", is_benchmark=False)
            ineligible = Ticker(symbol=_SYMBOLS[2], market="US", is_benchmark=False)
            benchmark = Ticker(symbol=_SYMBOLS[3], market="US", is_benchmark=True)
            session.add_all([current, missing, ineligible, benchmark])
            session.flush()
            session.add_all(
                [
                    _raw(current.id, "2026-09-07", "current"),
                    _raw(missing.id, None, "missing"),
                    _raw(ineligible.id, None, "ineligible"),
                    _raw(benchmark.id, None, "benchmark"),
                ]
            )

        statements: list[str] = []

        def record_select(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", record_select)
        try:
            with session_scope() as session:
                result = active_symbols_missing_statement_observation(
                    session,
                    datetime.date(2026, 9, 7),
                    eligible_symbols={_SYMBOLS[0], _SYMBOLS[1], _SYMBOLS[3]},
                )
        finally:
            event.remove(engine, "before_cursor_execute", record_select)

        assert result == [_SYMBOLS[1]]
        assert len(statements) == 1
    finally:
        _cleanup()
