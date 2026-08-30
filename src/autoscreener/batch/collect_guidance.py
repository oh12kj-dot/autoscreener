"""会社ガイダンスの収集バッチ(K-6)。

`filing_sections`(`section='ex99'`、8-K決算プレスリリースの添付)を読んで
`screening/guidance_extract.py` の純関数に通し、結果を `guidance` へ upsert する。
決算説明会トランスクリプトが有料な一方、ガイダンス原文はEDGARに無料で存在する
という非対称性がこの機能の存在理由(詳細は `guidance_extract.py` のdocstring)。

原則3:このバッチが書く `guidance` は `evaluate_gates` にも `scoring/` にも
一切読まれない。表示・チェックリスト・ノート起草のみが読者。

例外は銘柄単位で握ってログに残し、次の銘柄へ進む。
"""

from __future__ import annotations

import logging

from autoscreener.batch.collect_events import select_event_tickers
from autoscreener.dates import utc_today
from autoscreener.db.models import FilingSection, Guidance, Ticker
from autoscreener.db.session import session_scope
from autoscreener.screening.guidance_extract import parse_guidance

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 300


def collect_guidance(symbols: list[str] | None = None, *, limit: int = _DEFAULT_LIMIT) -> dict[str, int]:
    """追跡対象銘柄の8-K決算プレスリリースからガイダンスを抽出し `guidance` へ
    upsert する。

    `filing_sections` の読み取りにCIKは不要なので、追跡対象の選定は
    `collect_events.select_event_tickers`(CIK不要版)を流用する
    (`collect_filings.select_tracked_tickers` はCIKが無い銘柄を除外するため)。

    戻り値は {"tickers": n, "sections": n, "new_items": n, "existing": n,
    "failures": n}。
    """
    counts = {"tickers": 0, "sections": 0, "new_items": 0, "existing": 0, "failures": 0}
    today = utc_today()

    with session_scope() as session:
        if symbols:
            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_event_tickers(session, limit=limit)

        for ticker in tickers:
            counts["tickers"] += 1
            try:
                sections = (
                    session.query(FilingSection)
                    .filter(FilingSection.ticker_id == ticker.id, FilingSection.section == "ex99")
                    .all()
                )
                for section in sections:
                    counts["sections"] += 1
                    for item in parse_guidance(section.text):
                        existing = (
                            session.query(Guidance)
                            .filter_by(
                                accession_number=section.accession_number,
                                metric=item.metric,
                                period_label=item.period_label,
                            )
                            .one_or_none()
                        )
                        if existing is not None:
                            counts["existing"] += 1
                            continue
                        session.add(
                            Guidance(
                                ticker_id=ticker.id,
                                filed_date=section.filed_date,
                                accession_number=section.accession_number,
                                period_label=item.period_label,
                                metric=item.metric,
                                low_usd=item.low,
                                high_usd=item.high,
                                raw_text=item.raw_text,
                                collected_on=today,
                            )
                        )
                        counts["new_items"] += 1
            except Exception:
                logger.exception("%s: guidance collection failed", ticker.symbol)
                counts["failures"] += 1
                continue

    return counts
