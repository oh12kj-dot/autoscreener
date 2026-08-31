"""SEC提出書類の本文をItem単位に切り出して保存するバッチ(K-2)。

`filings` テーブルにはメタデータとURLしか無い(`db/models.py` の `Filing`
docstring参照:本文は保存しない設計)。ここでそのURLから本文を取得し、
`collectors/filing_text.split_sections()`(純関数)でItemごとに切って
`filing_sections` に保存する。顧客集中(K-3)・訴訟・希薄化条項など下流の
本文解析はすべてこのテーブルを読むだけで完結させ、EDGARへの再アクセス
(レート制限・30.3.1)を避けるのが狙い。

8-Kのうち `items` に '2.02'(決算発表)を含むものは、本体ではなく添付の
EX-99(プレスリリース)に実質的な情報がある。K-4(ガイダンス抽出)がこの
`section='ex99'` 行を読む入力になるので、Item番号での切り出しができない
8-Kであっても必ず保存する。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.collectors.edgar_client import EdgarClient, filing_file_url
from autoscreener.collectors.errors import CollectionError
from autoscreener.collectors.filing_text import split_sections
from autoscreener.config import get_settings, load_edgar_config
from autoscreener.dates import utc_today
from autoscreener.db.models import Filing, FilingSection, Ticker
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)

_DEFAULT_TICKER_LIMIT = 300
_DEFAULT_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "8-K", "DEF 14A"})

# 決算発表(item 2.02)8-Kは四半期ごとに来るので、追跡開始から溜まった全件を
# 処理すると際限が無い。直近N件に絞る(8件あれば約2年分:K-4がガイダンス
# 実績の突き合わせに使うのはせいぜい直近数四半期)。
_MAX_8K_PER_TICKER = 8

# index.json の item name から決算プレスリリース添付を見つける。EX-99.1 が
# 大半だが、EX-99.2 (スライド) 等もあり得るため "ex" + "99" の並びだけで拾う。
_EX99_NAME_RE = re.compile(r"ex-?99", re.IGNORECASE)


@dataclass(frozen=True)
class SectionSource:
    """本文取得経路の抽象化。テストではこれごと差し替えてネットワークに出ない
    ようにする(`batch/collect_supply.py` の `*_fetcher` 注入と同じ思想)。"""

    document_text: Callable[[str], tuple[str, bool]]
    filing_index: Callable[[str, str], list[dict[str, Any]]]
    file_url: Callable[[str, str, str], str]


def _default_source() -> SectionSource:
    """既定の取得経路(実EDGAR)。呼び出し時まで `EdgarClient` を作らない
    ——import時点で `EDGAR_USER_AGENT` が未設定でも、この関数を呼ばない限り
    モジュール全体がimportできるようにするため(テスト容易性)。"""
    settings = get_settings()
    config = load_edgar_config()
    client = EdgarClient(config, settings.edgar_user_agent or "")
    return SectionSource(
        document_text=client.fetch_document_text,
        filing_index=client.fetch_filing_index,
        file_url=filing_file_url,
    )


def _existing_sections(session: Session, accession_number: str) -> set[str]:
    rows = session.query(FilingSection.section).filter_by(accession_number=accession_number).all()
    return {r[0] for r in rows}


def _save_section(
    session: Session,
    ticker: Ticker,
    filing: Filing,
    section: str,
    text: str,
    source_url: str | None,
    today: Any,
) -> None:
    session.add(
        FilingSection(
            ticker_id=ticker.id,
            accession_number=filing.accession_number,
            form=filing.form,
            filed_date=filing.filed_date,
            section=section,
            text=text,
            char_count=len(text),
            source_url=source_url,
            extracted_on=today,
        )
    )


def _process_body_filing(
    session: Session,
    ticker: Ticker,
    filing: Filing,
    source: SectionSource,
    today: Any,
    counts: dict[str, int],
) -> None:
    """10-K/10-Q本体をItem単位に切り出して保存する。"""
    if not filing.document_url:
        counts["skipped_no_url"] += 1
        return

    target_keys = (
        {"item1", "item1a", "item3", "item7"} if filing.form.upper().startswith("10-K") else {"item1a", "item7"}
    )
    existing = _existing_sections(session, filing.accession_number)
    if target_keys <= existing:
        # 既に全セクション保存済み。本文の再取得(EDGARへの再アクセス)を
        # 避けるためここで打ち切る。
        counts["existing"] += len(target_keys)
        return

    text, _truncated = source.document_text(filing.document_url)
    sections = split_sections(text, filing.form)
    for key in target_keys:
        if key in existing:
            counts["existing"] += 1
            continue
        if key not in sections:
            # 「解析したが見つからなかった」。Filing.analysis と同じ方針で
            # 空文字は保存しない(=行を作らない)。
            counts["not_found"] += 1
            continue
        _save_section(session, ticker, filing, key, sections[key], filing.document_url, today)
        counts["new_sections"] += 1


def _process_ex99_filing(
    session: Session,
    ticker: Ticker,
    filing: Filing,
    source: SectionSource,
    today: Any,
    counts: dict[str, int],
) -> None:
    """決算発表8-KのEX-99添付をそのまま保存する(K-4への入力)。"""
    if "ex99" in _existing_sections(session, filing.accession_number):
        counts["existing"] += 1
        return

    items = source.filing_index(filing.cik, filing.accession_number)
    match = next((item for item in items if _EX99_NAME_RE.search(str(item.get("name") or ""))), None)
    if match is None:
        counts["no_ex99"] += 1
        return

    url = source.file_url(filing.cik, filing.accession_number, str(match["name"]))
    text, _truncated = source.document_text(url)
    if not text.strip():
        counts["no_ex99"] += 1
        return
    _save_section(session, ticker, filing, "ex99", text, url, today)
    counts["new_sections"] += 1


def _process_proxy_filing(session: Session, ticker: Ticker, filing: Filing,
                          source: SectionSource, today: Any, counts: dict[str, int]) -> None:
    """Store the latest DEF 14A as one source-preserving proxy section."""
    if "proxy" in _existing_sections(session, filing.accession_number):
        counts["existing"] += 1
        return
    if not filing.document_url:
        counts["skipped_no_url"] += 1
        return
    text, _truncated = source.document_text(filing.document_url)
    if not text.strip():
        counts["not_found"] += 1
        return
    _save_section(session, ticker, filing, "proxy", text, filing.document_url, today)
    counts["new_sections"] += 1


def collect_filing_sections(
    symbols: list[str] | None = None,
    *,
    limit: int = 300,
    forms: set[str] | None = None,
    fetcher: SectionSource | None = None,
) -> dict[str, int]:
    """追跡対象銘柄の直近10-K・最新10-Q・決算発表8-KのEX-99添付をItem単位で
    `filing_sections` に保存する。

    - 10-K/10-Qは `filings` テーブルから `filed_date` 最新の1件のみを対象にする
      (過去分の遡及取得はスコープ外——本文解析は現時点の判断材料としてのみ使う)。
    - 8-Kは `items` に "2.02" を含むものだけを対象にし、直近 `_MAX_8K_PER_TICKER`
      件に限定する。
    - `(accession_number, section)` が既に `filing_sections` にあればスキップする
      (`counts["existing"]` を加算)。
    - `CollectionError` 系の例外は**銘柄単位**で握って次のティッカーへ進む
      (1銘柄の失敗で全体を止めない)。

    戻り値: {"tickers", "new_sections", "existing", "not_found", "no_ex99",
    "skipped_no_url", "failures"}。
    """
    target_forms = forms or _DEFAULT_FORMS
    source = fetcher or _default_source()
    today = utc_today()
    counts = {
        "tickers": 0,
        "new_sections": 0,
        "existing": 0,
        "not_found": 0,
        "no_ex99": 0,
        "skipped_no_url": 0,
        "failures": 0,
    }

    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_tracked_tickers(session, limit=limit)

        for ticker in tickers:
            counts["tickers"] += 1
            try:
                if "10-K" in target_forms:
                    latest_10k = (
                        session.query(Filing)
                        .filter_by(ticker_id=ticker.id, form="10-K")
                        .order_by(Filing.filed_date.desc())
                        .first()
                    )
                    if latest_10k is not None:
                        _process_body_filing(session, ticker, latest_10k, source, today, counts)

                if "10-Q" in target_forms:
                    latest_10q = (
                        session.query(Filing)
                        .filter_by(ticker_id=ticker.id, form="10-Q")
                        .order_by(Filing.filed_date.desc())
                        .first()
                    )
                    if latest_10q is not None:
                        _process_body_filing(session, ticker, latest_10q, source, today, counts)

                if "8-K" in target_forms:
                    eightk_filings = (
                        session.query(Filing)
                        .filter_by(ticker_id=ticker.id, form="8-K")
                        .order_by(Filing.filed_date.desc())
                        .limit(_MAX_8K_PER_TICKER)
                        .all()
                    )
                    for filing in eightk_filings:
                        if "2.02" not in (filing.items or []):
                            continue
                        _process_ex99_filing(session, ticker, filing, source, today, counts)

                if "DEF 14A" in target_forms:
                    latest_proxy = (
                        session.query(Filing)
                        .filter_by(ticker_id=ticker.id, form="DEF 14A")
                        .order_by(Filing.filed_date.desc())
                        .first()
                    )
                    if latest_proxy is not None:
                        _process_proxy_filing(session, ticker, latest_proxy, source, today, counts)
            except CollectionError:
                logger.exception("%s: filing section collection failed", ticker.symbol)
                counts["failures"] += 1
                continue

    return counts
