"""上場廃止ユニバース構築のテスト(defect_and_edge_audit_2026-08-28.md D-1 / I-2)。

パースは純粋関数。DB反映は ZZ*** シンボルで後片付けする。
"""

from __future__ import annotations

import datetime

from autoscreener.collectors.delisting_source import (
    DelistingEvent,
    iter_delisting_events,
    parse_form_index,
    register_delisting_events,
)
from autoscreener.db.models import Ticker
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


def _cleanup(symbols: list[str]) -> None:
    with session_scope() as session:
        for ticker in session.query(Ticker).filter(Ticker.symbol.in_(symbols)).all():
            session.delete(ticker)
