"""週次CIK突合(30.3.2)。

`company_tickers.json` を取得し、`tickers.cik` を埋める。**突合はシンボル
文字列で行う。** SECは `BRK-B` 形式、NASDAQ Trader は `BRK.B` 形式のことが
あるため、`symbols.py` の表記ゆれ正規化を使う(`tradability.py` と共通)。

**1シンボルが複数CIKに当たる場合は埋めない**(曖昧なまま埋めると別会社の
書類を読むことになる)。`unmatched` に数え、ログにWARNINGを出す。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.db.models import Ticker
from autoscreener.db.session import session_scope
from autoscreener.symbols import symbol_variants

logger = logging.getLogger(__name__)


def _build_reverse_map(symbol_to_cik: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """{正規化シンボルの表記ゆれ: CIK} を組み立てる。

    表記ゆれ両方(BRK.B / BRK-B)を同じCIKに写像するが、**別のCIKへ写像しよう
    とした場合は曖昧とみなしてどちらも除外する**(unmatched に加える)。
    """
    expanded: dict[str, set[str]] = {}
    for symbol, cik in symbol_to_cik.items():
        for variant in symbol_variants(symbol):
            expanded.setdefault(variant, set()).add(cik)

    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()
    for variant, ciks in expanded.items():
        if len(ciks) == 1:
            resolved[variant] = next(iter(ciks))
        else:
            ambiguous.add(variant)
    return resolved, ambiguous


def refresh_cik_map(session: Session | None = None) -> dict[str, int]:
    """`company_tickers.json` を取得し、`tickers.cik` を埋める。

    戻り値は {"matched": n, "unmatched": n, "updated": n}。
    週次(月曜・ユニバース再取得と同じ日)に実行する。
    """
    settings = get_settings()
    edgar_config = load_edgar_config()
    client = EdgarClient(edgar_config, settings.edgar_user_agent or "")

    symbol_to_cik = client.fetch_company_tickers()
    resolved, ambiguous = _build_reverse_map(symbol_to_cik)

    def _run(s: Session) -> dict[str, int]:
        tickers = s.query(Ticker).filter(Ticker.delisted_at.is_(None)).all()
        matched = 0
        unmatched = 0
        updated = 0
        for ticker in tickers:
            variants = symbol_variants(ticker.symbol)
            if variants & ambiguous:
                unmatched += 1
                logger.warning("%s: symbol maps to multiple CIKs, leaving cik unset", ticker.symbol)
                continue
            cik = next((resolved[v] for v in variants if v in resolved), None)
            if cik is None:
                unmatched += 1
                continue
            matched += 1
            if ticker.cik != cik:
                ticker.cik = cik
                updated += 1
        return {"matched": matched, "unmatched": unmatched, "updated": updated}

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
