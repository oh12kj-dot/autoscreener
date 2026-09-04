"""tests/unit/test_collect_concentration.py(S-5、
docs/daily_pipeline_throughput_plan_2026-09-04.md)。

`collect_concentration`は以前`for ticker in tickers:`の完全な逐次ループ
だった。銘柄ごとに専用の`session_scope()`を開いて共有`sec`リミッター配下で
並列化するようにした。ここでは複数銘柄が正しく解決され、
`filing_sections`本文からの抽出結果が正しく`customer_concentration`へ
書き込まれ、集計が銘柄をまたいで合算されることを確認する。

`xbrl_fetcher`は既存の注入経路をそのまま使い、ネットワークには一切出ない。
DBに触れる(ローカル開発用Postgres)。
"""

from __future__ import annotations

import datetime

from autoscreener.batch.collect_concentration import collect_concentration
from autoscreener.db.models import CustomerConcentration, Filing, FilingSection, Ticker
from autoscreener.db.session import session_scope

_SYMBOLS = ["ZZCONC1", "ZZCONC2"]
_TEXT = "Customer A represented approximately 15.4% of our total revenues in fiscal 2025."


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(_SYMBOLS)).all()
        for ticker in tickers:
            session.query(CustomerConcentration).filter_by(ticker_id=ticker.id).delete()
            session.query(FilingSection).filter_by(ticker_id=ticker.id).delete()
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


def _no_xbrl(cik: str) -> dict:
    return {}


def test_collect_concentration_resolves_multiple_symbols_and_aggregates_counts():
    _cleanup()
    try:
        report_date = datetime.date(2025, 12, 31)
        with session_scope() as session:
            for idx, symbol in enumerate(_SYMBOLS):
                ticker = Ticker(symbol=symbol, market="US", cik=f"000000000{idx + 1}")
                session.add(ticker)
                session.flush()
                accession = f"0001-99-00000{idx + 1}"
                session.add(
                    Filing(
                        ticker_id=ticker.id, cik=ticker.cik, accession_number=accession,
                        form="10-K", filed_date=report_date, report_date=report_date,
                    )
                )
                session.add(
                    FilingSection(
                        ticker_id=ticker.id, accession_number=accession, form="10-K",
                        filed_date=report_date, section="item1", text=_TEXT,
                        char_count=len(_TEXT), extracted_on=datetime.date(2026, 1, 15),
                    )
                )

        stats = collect_concentration(symbols=_SYMBOLS, xbrl_fetcher=_no_xbrl)
        assert stats["tickers"] == len(_SYMBOLS)
        assert stats["new_rows"] == len(_SYMBOLS)
        assert stats["failures"] == 0

        with session_scope() as session:
            for symbol in _SYMBOLS:
                ticker = session.query(Ticker).filter_by(symbol=symbol).one()
                row = session.query(CustomerConcentration).filter_by(ticker_id=ticker.id).one()
                assert row.customer_label == "Customer A"
                assert float(row.revenue_pct) == 0.154
                assert row.source == "text"
    finally:
        _cleanup()


def test_collect_concentration_no_filing_sections_is_not_a_failure():
    symbol = "ZZCONC1"
    _cleanup()
    try:
        with session_scope() as session:
            session.add(Ticker(symbol=symbol, market="US"))

        stats = collect_concentration(symbols=[symbol], xbrl_fetcher=_no_xbrl)
        assert stats["tickers"] == 1
        assert stats["no_filing_sections"] == 1
        assert stats["failures"] == 0
        assert stats["new_rows"] == 0
    finally:
        _cleanup()
