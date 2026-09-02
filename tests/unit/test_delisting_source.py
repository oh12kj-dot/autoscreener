"""上場廃止ユニバース構築のテスト(docs/defect_and_edge_audit_2026-08-28.md D-1 / I-2)。

パースは純粋関数。DB反映は ZZ*** シンボルで後片付けする。
"""

from __future__ import annotations

import datetime

from autoscreener.collectors.delisting_source import (
    DELISTING_TRADING_GRACE_DAYS,
    DelistingEvent,
    iter_delisting_events,
    last_trade_after_delisting,
    parse_form_index,
    register_delisting_events,
)
from autoscreener.batch.collect_delistings import rollback_false_delistings
from autoscreener.db.models import DelistingEvent as DelistingEventRow, PriceSnapshot, Ticker
from autoscreener.db.session import session_scope

_SAMPLE_IDX = """Description:           Master Index of EDGAR Dissemination Feed by Form Type

 Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
25          ACME MICROCAP CORP                                            0001234567  2024-03-15  edgar/data/1234567/0001234567-24-000012.txt
25-NSE      OLD WIDGETS INC                                               0000998877  2024-05-02  edgar/data/998877/0000998877-24-000003.txt
10-K        STILL LISTED CO                                               0000111222  2024-02-20  edgar/data/111222/0000111222-24-000009.txt
15-12B      GONE PRIVATE LTD                                              0000777888  2023-11-30  edgar/data/777888/0000777888-23-000044.txt
8-K         SOMETHING HAPPENED CORP                                       0000333444  2024-01-10  edgar/data/333444/0000333444-24-000002.txt
"""


def test_parse_form_index_reads_records_after_the_separator():
    entries = parse_form_index(_SAMPLE_IDX)
    forms = [e.form for e in entries]
    assert forms == ["25", "25-NSE", "10-K", "15-12B", "8-K"]
    acme = entries[0]
    assert acme.cik == "0001234567"
    assert acme.company == "ACME MICROCAP CORP"
    assert acme.date_filed == datetime.date(2024, 3, 15)


def test_iter_delisting_events_keeps_only_delisting_forms():
    events = list(iter_delisting_events(parse_form_index(_SAMPLE_IDX)))
    assert {e.form for e in events} == {"25", "25-NSE", "15-12B"}
    assert all(isinstance(e, DelistingEvent) for e in events)


def test_parse_handles_company_names_with_spaces_and_blank_lines():
    idx = (
        "Form Type   Company Name   CIK   Date Filed   File Name\n"
        "-----------------------------------------------------------\n"
        "\n"
        "25          A B C HOLDINGS  GROUP INC   0000005555   2025-06-01   edgar/data/5555/x.txt\n"
    )
    entries = parse_form_index(idx)
    assert len(entries) == 1
    assert entries[0].company == "A B C HOLDINGS GROUP INC"
    assert entries[0].cik == "0000005555"


def test_register_delisting_events_sets_delisted_at_without_quarantine():
    symbols = ["ZZDL1", "ZZDL2"]
    ciks = {"0000900001": "ZZDL1", "0000900002": "ZZDL2"}
    _cleanup(symbols)
    try:
        events = [
            DelistingEvent("0000900001", "25", datetime.date(2024, 4, 1), "ZZ DL ONE"),
            # 同一シンボルに古いイベント -> こちらが採用される
            DelistingEvent("0000900001", "15-12B", datetime.date(2024, 1, 5), "ZZ DL ONE"),
            DelistingEvent("0000900002", "25-NSE", datetime.date(2023, 12, 20), "ZZ DL TWO"),
        ]
        counts = register_delisting_events(events, cik_to_symbol=dict(ciks))
        assert counts["registered"] == 2

        with session_scope() as session:
            rows = {t.symbol: t for t in session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all()}
            assert rows["ZZDL1"].delisted_at.date() == datetime.date(2024, 1, 5)  # 最古
            assert rows["ZZDL1"].is_quarantined is False
            assert rows["ZZDL2"].delisted_at.date() == datetime.date(2023, 12, 20)
    finally:
        _cleanup(symbols)


def test_last_trade_after_delisting_respects_the_grace_window():
    """判定基準そのもの(タスク①②で共有):廃止日 + 猶予日数より後に約定が
    あるときだけ最終取引日を返す。猶予内・以前で途切れているものは None。"""
    symbols = ["ZZLTAD1", "ZZLTAD2", "ZZLTAD3"]
    _cleanup(symbols)
    claimed = datetime.date(2024, 4, 1)
    try:
        with session_scope() as session:
            gone = Ticker(symbol="ZZLTAD1", market="US")       # 廃止日以前で終了
            within = Ticker(symbol="ZZLTAD2", market="US")      # 猶予窓の内側
            trading = Ticker(symbol="ZZLTAD3", market="US")     # 猶予窓より後
            session.add_all([gone, within, trading])
            session.flush()
            session.add_all([
                PriceSnapshot(ticker_id=gone.id, trade_date=claimed - datetime.timedelta(days=3), close=1.0, volume=10),
                PriceSnapshot(
                    ticker_id=within.id,
                    trade_date=claimed + datetime.timedelta(days=DELISTING_TRADING_GRACE_DAYS - 1),
                    close=1.0,
                    volume=10,
                ),
                PriceSnapshot(
                    ticker_id=trading.id,
                    trade_date=claimed + datetime.timedelta(days=DELISTING_TRADING_GRACE_DAYS + 1),
                    close=1.0,
                    volume=10,
                ),
            ])
            session.flush()

            assert last_trade_after_delisting(session, gone.id, claimed) is None
            assert last_trade_after_delisting(session, within.id, claimed) is None
            assert last_trade_after_delisting(session, trading.id, claimed) == (
                claimed + datetime.timedelta(days=DELISTING_TRADING_GRACE_DAYS + 1)
            )
    finally:
        _cleanup(symbols)


def test_register_delisting_events_skips_symbols_still_trading():
    """ガード(2026-09-02 D-1):提出日より後に価格がある銘柄は廃止登録しない。

    上場ノートの個別シリーズ償還で Form 25 を出す発行体(AAPL・MA 等)を
    CIK→シンボル解決で本体ごと廃止扱いにする誤検出を、登録の手前で弾く。
    """
    still_trading, truly_gone = "ZZGUARD1", "ZZGUARD2"
    _cleanup([still_trading, truly_gone])
    filed = datetime.date(2024, 4, 1)
    try:
        with session_scope() as session:
            t1 = Ticker(symbol=still_trading, market="US")
            t2 = Ticker(symbol=truly_gone, market="US")
            session.add_all([t1, t2])
            session.flush()
            session.add_all([
                # 提出日の半年後まで取引継続 -> ガードで据え置き
                PriceSnapshot(ticker_id=t1.id, trade_date=filed + datetime.timedelta(days=180), close=12.0, volume=1000),
                # 取引は提出日の10日前で終了 -> 本物の廃止として登録される
                PriceSnapshot(ticker_id=t2.id, trade_date=filed - datetime.timedelta(days=10), close=0.4, volume=500),
            ])

        events = [
            DelistingEvent("0000930001", "25", filed, "ZZ GUARD ONE"),
            DelistingEvent("0000930002", "25", filed, "ZZ GUARD TWO"),
        ]
        counts = register_delisting_events(
            events, cik_to_symbol={"0000930001": still_trading, "0000930002": truly_gone}
        )

        assert counts["skipped_recent_trading"] == 1
        with session_scope() as session:
            rows = {
                t.symbol: t
                for t in session.query(Ticker).filter(Ticker.symbol.in_([still_trading, truly_gone])).all()
            }
            assert rows[still_trading].delisted_at is None
            assert (
                session.query(DelistingEventRow).filter_by(ticker_id=rows[still_trading].id).count() == 0
            )
            assert rows[truly_gone].delisted_at.date() == filed
            assert (
                session.query(DelistingEventRow).filter_by(ticker_id=rows[truly_gone].id).count() == 1
            )
    finally:
        _cleanup([still_trading, truly_gone])


def test_rollback_false_delisting_is_dry_run_by_default_and_applies_when_requested():
    symbol = "ZZROLLBACK"
    _cleanup([symbol])
    claimed = datetime.date(2024, 4, 1)
    try:
        with session_scope() as session:
            ticker = Ticker(
                symbol=symbol, market="US",
                delisted_at=datetime.datetime.combine(claimed, datetime.time(), tzinfo=datetime.timezone.utc),
            )
            session.add(ticker)
            session.flush()
            session.add(PriceSnapshot(
                ticker_id=ticker.id, trade_date=claimed + datetime.timedelta(days=180),
                close=8.0, volume=100,
            ))
            session.add(DelistingEventRow(
                ticker_id=ticker.id, event_date=claimed, event_type="unknown",
                source="test", observed_at=datetime.datetime.now(datetime.timezone.utc), confidence="low",
            ))

        dry = rollback_false_delistings(symbols=[symbol])
        assert dry == {"delisted_total": 1, "rolled_back": 1, "reserved": 0, "events_deleted": 1}
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            assert ticker.delisted_at is not None

        applied = rollback_false_delistings(apply=True, symbols=[symbol])
        assert applied["rolled_back"] == 1
        with session_scope() as session:
            ticker = session.query(Ticker).filter_by(symbol=symbol).one()
            assert ticker.delisted_at is None
            assert session.query(DelistingEventRow).filter_by(ticker_id=ticker.id).count() == 0
    finally:
        _cleanup([symbol])


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        for ticker in session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all():
            session.query(DelistingEventRow).filter_by(ticker_id=ticker.id).delete()
            session.query(PriceSnapshot).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)
