"""tests/unit/test_collect_filing_sections.py(S-5、
docs/daily_pipeline_throughput_plan_2026-09-04.md)。

`collect_filing_sections`は以前`for ticker in tickers:`の完全な逐次ループ
だった。銘柄ごとに専用の`session_scope()`を開いて共有`sec`リミッター配下で
並列化するようにした。ここでは複数銘柄が正しく解決され、決算発表8-Kの
EX-99添付が正しく`filing_sections`へ保存され、集計が銘柄をまたいで
合算されることを確認する(EX-99経路は`split_sections`の見出し正規表現に
依存しないため、複雑な10-K本文を用意せずに済む——本文切り出し自体の
正しさは既存の`collectors/filing_text.py`のテストが担う)。

`fetcher`(`SectionSource`)は既存の注入経路をそのまま使い、ネットワークには
一切出ない。DBに触れる(ローカル開発用Postgres)。
"""

from __future__ import annotations

import datetime

from autoscreener.batch.collect_filing_sections import SectionSource, collect_filing_sections
from autoscreener.db.models import Filing, FilingSection, SourceProcessingLedger, Ticker
from autoscreener.db.session import session_scope

_SYMBOLS = ["ZZSEC1", "ZZSEC2"]
_EX99_TEXT = "Q3 revenue grew 20% year over year."


def _cleanup() -> None:
    with session_scope() as session:
        session.query(SourceProcessingLedger).filter(
            SourceProcessingLedger.source_type == "filing",
            SourceProcessingLedger.source_key.like("0001-99-%"),
        ).delete(synchronize_session=False)
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(_SYMBOLS)).all()
        for ticker in tickers:
            session.query(FilingSection).filter_by(ticker_id=ticker.id).delete()
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


def _fake_source(calls: list[str]) -> SectionSource:
    def filing_index(cik: str, accession: str):
        calls.append(f"index:{cik}:{accession}")
        return [{"name": "ex991.htm", "type": "8-K"}]

    def file_url(cik: str, accession: str, filename: str) -> str:
        return f"https://example.test/{cik}/{accession}/{filename}"

    def document_text(url: str):
        calls.append(f"text:{url}")
        return _EX99_TEXT, False

    return SectionSource(document_text=document_text, filing_index=filing_index, file_url=file_url)


def test_collect_filing_sections_resolves_multiple_symbols_and_aggregates_counts():
    _cleanup()
    try:
        filed_date = datetime.date(2026, 7, 1)
        with session_scope() as session:
            for idx, symbol in enumerate(_SYMBOLS):
                ticker = Ticker(symbol=symbol, market="US", cik=f"000000000{idx + 1}")
                session.add(ticker)
                session.flush()
                session.add(
                    Filing(
                        ticker_id=ticker.id, cik=ticker.cik, accession_number=f"0001-99-00000{idx + 1}",
                        form="8-K", filed_date=filed_date, items=["2.02"],
                    )
                )

        calls: list[str] = []
        stats = collect_filing_sections(symbols=_SYMBOLS, forms={"8-K"}, fetcher=_fake_source(calls))

        assert stats["tickers"] == len(_SYMBOLS)
        assert stats["new_sections"] == len(_SYMBOLS)
        assert stats["failures"] == 0
        # 両銘柄それぞれについて filing_index → document_text が呼ばれたこと
        assert sum(1 for c in calls if c.startswith("index:")) == len(_SYMBOLS)
        assert sum(1 for c in calls if c.startswith("text:")) == len(_SYMBOLS)

        with session_scope() as session:
            for symbol in _SYMBOLS:
                ticker = session.query(Ticker).filter_by(symbol=symbol).one()
                row = session.query(FilingSection).filter_by(ticker_id=ticker.id, section="ex99").one()
                assert row.text == _EX99_TEXT
    finally:
        _cleanup()


def test_collect_filing_sections_skips_8k_without_earnings_item():
    symbol = "ZZSEC1"
    _cleanup()
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US", cik="0000000001")
            session.add(ticker)
            session.flush()
            session.add(
                Filing(
                    ticker_id=ticker.id, cik=ticker.cik, accession_number="0001-99-000099",
                    form="8-K", filed_date=datetime.date(2026, 7, 1), items=["5.02"],  # 決算発表ではない
                )
            )

        calls: list[str] = []
        stats = collect_filing_sections(symbols=[symbol], forms={"8-K"}, fetcher=_fake_source(calls))
        assert stats["tickers"] == 1
        assert stats["new_sections"] == 0
        assert not calls  # フェッチャーに一切触れていない
    finally:
        _cleanup()


def test_no_ex99_result_is_cached_and_not_requested_again():
    symbol = "ZZSEC1"
    _cleanup()
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US", cik="0000000001")
            session.add(ticker)
            session.flush()
            session.add(Filing(
                ticker_id=ticker.id,
                cik=ticker.cik,
                accession_number="0001-99-000098",
                form="8-K",
                filed_date=datetime.date(2026, 7, 2),
                items=["2.02"],
            ))

        calls: list[str] = []

        def filing_index(cik: str, accession: str):
            calls.append(accession)
            return []

        source = SectionSource(
            document_text=lambda _url: ("", False),
            filing_index=filing_index,
            file_url=lambda cik, accession, filename: f"https://example.test/{filename}",
        )
        first = collect_filing_sections(symbols=[symbol], forms={"8-K"}, fetcher=source)
        second = collect_filing_sections(symbols=[symbol], forms={"8-K"}, fetcher=source)
        assert first["no_ex99"] == 1
        assert second["existing"] == 1
        assert calls == ["0001-99-000098"]
    finally:
        _cleanup()
