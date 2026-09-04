"""tests/unit/test_collect_dilution.py(S-5、
docs/daily_pipeline_throughput_plan_2026-09-04.md)。

`collect_dilution`は以前`for ticker in tickers:`の完全な逐次ループだった。
銘柄ごとに専用の`session_scope()`を開いて共有`sec`リミッター配下で
並列化するようにした。ここでは:

1. `_process_ticker`単体で、従来どおりS-3の表紙から`shelf_registered_usd`を
   抽出して`dilution_capacity`へ書き込めること(1銘柄ぶんのロジック自体は
   変えていないことの確認)。
2. `collect_dilution`が複数銘柄を正しく解決し、`run_parallel_tickers`
   (S-5の共通並列実行ヘルパー)へワイヤリングされていること。

`EdgarClient`は`collect_dilution()`本体が直接構築する(注入できない)ため、
2.は`autoscreener.batch.collect_dilution.EdgarClient`をパッチして
ネットワークに出ないようにする。DBに触れる(ローカル開発用Postgres)。
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from autoscreener.batch.collect_dilution import _process_ticker, collect_dilution
from autoscreener.db.models import DilutionCapacity, Filing, Ticker
from autoscreener.db.session import session_scope

_SYMBOLS = ["ZZDIL1", "ZZDIL2"]
_S3_TEXT = "We are registering securities with an aggregate offering price of $150.0 million."


def _cleanup() -> None:
    with session_scope() as session:
        tickers = session.query(Ticker).filter(Ticker.symbol.in_(_SYMBOLS)).all()
        for ticker in tickers:
            session.query(DilutionCapacity).filter_by(ticker_id=ticker.id).delete()
            session.query(Filing).filter_by(ticker_id=ticker.id).delete()
            session.delete(ticker)


class _FakeEdgarClient:
    """`fetch_document_text`/`fetch_company_concept`だけを実装する軽量な
    テストダブル。ネットワークに一切出ない。"""

    def __init__(self, text_by_url: dict[str, str]):
        self._text_by_url = text_by_url

    def fetch_document_text(self, url: str, **_kwargs):
        return self._text_by_url.get(url, ""), False

    def fetch_company_concept(self, cik: str, taxonomy: str, tag: str):
        return {}


def test_process_ticker_writes_dilution_capacity_from_shelf_filing():
    """`_process_ticker`単体の正しさ(並列化前と同じロジックであること)。"""
    symbol = "ZZDIL1"
    _cleanup()
    try:
        today = datetime.date(2099, 6, 1)
        url = "https://www.sec.gov/Archives/edgar/data/1/x/s3.htm"
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US", cik="0000000001")
            session.add(ticker)
            session.flush()
            ticker_id = ticker.id
            session.add(
                Filing(
                    ticker_id=ticker_id, cik=ticker.cik, accession_number="0001-99-000001",
                    form="S-3", filed_date=today - datetime.timedelta(days=10), document_url=url,
                )
            )

        client = _FakeEdgarClient({url: _S3_TEXT})
        with session_scope() as session:
            result = _process_ticker(session, ticker_id, client, today)

        assert result == {"tickers": 1, "written": 1, "skipped_no_cik": 0, "failures": 0}

        with session_scope() as session:
            row = session.query(DilutionCapacity).filter_by(ticker_id=ticker_id).one()
            assert float(row.shelf_registered_usd) == 150_000_000.0
    finally:
        _cleanup()


def test_process_ticker_skips_tickers_without_cik():
    symbol = "ZZDIL2"
    _cleanup()
    try:
        with session_scope() as session:
            ticker = Ticker(symbol=symbol, market="US")  # cik未設定
            session.add(ticker)
            session.flush()
            ticker_id = ticker.id

        client = _FakeEdgarClient({})
        with session_scope() as session:
            result = _process_ticker(session, ticker_id, client, datetime.date(2099, 6, 1))

        assert result == {"tickers": 0, "written": 0, "skipped_no_cik": 1, "failures": 0}
    finally:
        _cleanup()


@patch("autoscreener.batch.collect_dilution.EdgarClient")
def test_collect_dilution_resolves_multiple_symbols_and_aggregates_counts(mock_edgar_client_cls):
    """`collect_dilution(symbols=[...])`が複数銘柄を解決し、`run_parallel_tickers`
    経由で集計まで行うこと(S-5のワイヤリング確認)。"""
    _cleanup()
    try:
        # `collect_dilution`は`utc_today()`(実際の現在日)を基準に直近
        # `_LOOKBACK_YEARS`年分のfilingsを対象にするので、未来日ではなく
        # 実際の「少し前」の日付を使う。
        recent_filed_date = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=10)
        urls = {}
        with session_scope() as session:
            for idx, symbol in enumerate(_SYMBOLS):
                cik = f"000000000{idx + 1}"
                ticker = Ticker(symbol=symbol, market="US", cik=cik)
                session.add(ticker)
                session.flush()
                url = f"https://www.sec.gov/Archives/edgar/data/{idx + 1}/x/s3.htm"
                urls[url] = _S3_TEXT
                session.add(
                    Filing(
                        ticker_id=ticker.id, cik=cik, accession_number=f"0001-99-00000{idx + 1}",
                        form="S-3", filed_date=recent_filed_date, document_url=url,
                    )
                )

        mock_edgar_client_cls.return_value = _FakeEdgarClient(urls)

        stats = collect_dilution(symbols=_SYMBOLS)
        assert stats["tickers"] == len(_SYMBOLS)
        assert stats["written"] == len(_SYMBOLS)
        assert stats["failures"] == 0
    finally:
        _cleanup()
