"""需給データの収集(J-7、docs/investment_decision_gap_2026-08-29.md)。

Form 4(インサイダー取引)と FINRA の空売り残。パーサは実装済み
(`collectors/form4_source.py` / `collectors/short_interest_source.py`)。ここは
**保存先・バッチ**の担当。

**原則3 の徹底**:これらは `evaluate_gates` にもスコアにも入れない。表示と
アラートのみ。追跡対象銘柄(30.3.4)に限定し、週次(月曜)で回す。

実際の EDGAR / FINRA からの取得は `*_fetcher` 引数で注入できる(テストや
オフライン運用のため)。**K-7でこのファイルの既定 fetcher を実データ経路に
差し替えた**——`collectors/form4_fetch.py` / `collectors/short_interest_fetch.py`
がそれぞれ EDGAR の Form 4 / FINRA の空売り残ファイルを実際に取得する。
`EDGAR_USER_AGENT` 未設定などで前提が欠けている環境では、例外を投げず
空リストを返す(警告ログはバッチ実行あたり1回だけ——ティッカー数ぶん
ログが埋まらないようにする)という既存の挙動は変えていない。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from autoscreener.collectors.errors import CollectionError
from autoscreener.collectors.short_interest_source import ShortInterestRecord
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.dates import utc_today
from autoscreener.db.models import InsiderTransaction, ShortInterest, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

_DEFAULT_TICKER_LIMIT = 300


@dataclass(frozen=True)
class InsiderRow:
    """`insider_transactions` の1行に対応する収集結果。"""

    accession_number: str
    transaction_date: datetime.date
    insider_name: str
    transaction_code: str
    shares: float
    filed_date: datetime.date | None = None
    role: str | None = None
    price_usd: float | None = None
    value_usd: float | None = None
    is_derivative: bool = False


InsiderFetcher = Callable[[Ticker], list[InsiderRow]]
ShortInterestFetcher = Callable[[Ticker], list[ShortInterestRecord]]


def _no_insider(_ticker: Ticker) -> list[InsiderRow]:
    logger.info("insider fetcher not configured; no Form 4 data collected for %s", _ticker.symbol)
    return []


def _no_short_interest(_ticker: Ticker) -> list[ShortInterestRecord]:
    logger.info("short-interest fetcher not configured; nothing collected for %s", _ticker.symbol)
    return []


def _default_insider_fetcher() -> InsiderFetcher:
    """既定のインサイダー取得経路を組み立てる(K-7)。

    `EdgarClient` の構築は `EDGAR_USER_AGENT` 未設定だと `ValueError` を投げる
    (`collectors/edgar_client.py`)。ここで一度だけ捕まえて `_no_insider` に
    フォールバックすることで、「未設定でも0件で通る」という既存の呼び出し側の
    前提(cli.py 側の配線含む)を壊さない。警告ログは
    `collect_insider()` 呼び出しあたり1回(この関数が呼ばれる回数)だけ出る
    ——ティッカー数(最大300)ぶん出て埋もれる、ということが起きないようにする。
    """
    from autoscreener.collectors.edgar_client import EdgarClient
    from autoscreener.collectors.form4_fetch import fetch_form4_rows

    try:
        client = EdgarClient(load_edgar_config(), get_settings().edgar_user_agent or "")
    except ValueError:
        logger.warning(
            "EDGAR_USER_AGENT が未設定のため insider fetcher を無効化します"
            "(Form4データは収集されません。空リストで通します)"
        )
        return _no_insider

    def _fetch(ticker: Ticker) -> list[InsiderRow]:
        if not ticker.cik:
            return []
        try:
            return fetch_form4_rows(client, ticker.cik)
        except CollectionError:
            logger.warning("%s: Form4取得に失敗", ticker.symbol, exc_info=True)
            return []

    return _fetch


def _default_short_interest_fetcher() -> ShortInterestFetcher:
    """既定の空売り残取得経路を組み立てる(K-7)。

    **FINRAのファイルは全銘柄が1本にまとまっている**ため、ここで
    `fetch_short_interest_all()` を1回だけ呼び、シンボル別の辞書に索いておく。
    各ティッカー呼び出しはその辞書を引くだけのクロージャにすることで、
    `collect_short_interest()` が300ティッカーぶんダウンロードし直す事態を防ぐ
    (タスク仕様どおり「関数の入口で1回だけ全体を取る」構造)。
    ダウンロード自体が失敗した場合(ネットワーク不通等)も例外を投げず、
    警告ログ1回のあと空の辞書で継続する。
    """
    from autoscreener.collectors.short_interest_fetch import fetch_short_interest_all

    try:
        by_symbol = fetch_short_interest_all()
    except Exception:  # noqa: BLE001 — FINRA側の予期しない失敗でバッチ全体を止めない
        logger.warning("FINRA空売り残の取得に失敗しました。今回は空で継続します", exc_info=True)
        by_symbol = {}

    def _fetch(ticker: Ticker) -> list[ShortInterestRecord]:
        return by_symbol.get(ticker.symbol.upper(), [])

    return _fetch


def _target_tickers(session: Session, symbols: list[str] | None, limit: int) -> list[Ticker]:
    if symbols:
        return session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
    # collect_events と同じ追跡対象選定を流用する。
    from autoscreener.batch.collect_events import select_event_tickers

    return select_event_tickers(session, limit=limit)


def collect_insider(
    symbols: list[str] | None = None,
    *,
    limit: int = _DEFAULT_TICKER_LIMIT,
    fetcher: InsiderFetcher | None = None,
) -> dict[str, int]:
    """Form 4 取引を `insider_transactions` に upsert する。戻り値は件数。

    `fetcher` を省略すると `_default_insider_fetcher()`(実際に EDGAR から
    取得する経路)を使う。テストやオフライン運用では明示的に注入すること。
    """
    resolved_fetcher = fetcher if fetcher is not None else _default_insider_fetcher()
    counts = {"tickers": 0, "new_rows": 0, "existing": 0}
    with session_scope() as session:
        for ticker in _target_tickers(session, symbols, limit):
            counts["tickers"] += 1
            for row in resolved_fetcher(ticker):
                exists = (
                    session.query(InsiderTransaction)
                    .filter_by(
                        accession_number=row.accession_number,
                        insider_name=row.insider_name,
                        transaction_date=row.transaction_date,
                        transaction_code=row.transaction_code,
                        shares=row.shares,
                    )
                    .first()
                )
                if exists is not None:
                    counts["existing"] += 1
                    continue
                session.add(
                    InsiderTransaction(
                        ticker_id=ticker.id,
                        accession_number=row.accession_number,
                        filed_date=row.filed_date,
                        transaction_date=row.transaction_date,
                        insider_name=row.insider_name,
                        role=row.role,
                        transaction_code=row.transaction_code,
                        shares=row.shares,
                        price_usd=row.price_usd,
                        value_usd=row.value_usd,
                        is_derivative=row.is_derivative,
                    )
                )
                counts["new_rows"] += 1
    return counts


def collect_short_interest(
    symbols: list[str] | None = None,
    *,
    limit: int = _DEFAULT_TICKER_LIMIT,
    fetcher: ShortInterestFetcher | None = None,
    as_of: datetime.date | None = None,
) -> dict[str, int]:
    """FINRA 空売り残を `short_interest` に upsert する。

    `fetcher` を省略すると `_default_short_interest_fetcher()` を使う——
    その内部で FINRA の全銘柄ファイルを1回だけダウンロードしてから
    ティッカーごとのクロージャを組み立てる(モジュールdocstring参照)。
    """
    today = as_of or utc_today()
    resolved_fetcher = fetcher if fetcher is not None else _default_short_interest_fetcher()
    counts = {"tickers": 0, "new_rows": 0, "existing": 0}
    with session_scope() as session:
        for ticker in _target_tickers(session, symbols, limit):
            counts["tickers"] += 1
            for record in resolved_fetcher(ticker):
                exists = (
                    session.query(ShortInterest)
                    .filter_by(ticker_id=ticker.id, settlement_date=record.settlement_date)
                    .first()
                )
                if exists is not None:
                    counts["existing"] += 1
                    continue
                session.add(
                    ShortInterest(
                        ticker_id=ticker.id,
                        settlement_date=record.settlement_date,
                        short_interest_shares=record.current_short_shares,
                        avg_daily_volume=record.avg_daily_volume,
                        days_to_cover=record.days_to_cover(),
                        published_date=today,
                    )
                )
                counts["new_rows"] += 1
    return counts
