"""SEC XBRL実績値の収集バッチ(30.5.5)。週次(月曜)実行を想定——財務データは
四半期に1回しか変わらないので日次は無駄。

追跡対象銘柄(30.3.4)の companyfacts を取り、4概念ぶんを upsert する。
ユニバース全件のcompanyfactsを毎日取るのは非現実的なため、突合は追跡対象
銘柄だけで行う(30.5.3)。
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert

from autoscreener.batch.collect_filings import select_tracked_tickers
from autoscreener.collectors.edgar_client import EdgarClient
from autoscreener.collectors.errors import CollectionError, EmptyResponseError
from autoscreener.config import EdgarConfig, get_settings, load_edgar_config
from autoscreener.db.models import XbrlFact
from autoscreener.db.session import session_scope
from autoscreener.validation.xbrl_facts import extract_all_concepts

logger = logging.getLogger(__name__)


def collect_xbrl_facts(
    *, symbols: list[str] | None = None, edgar_config: EdgarConfig | None = None
) -> dict[str, int]:
    """追跡対象銘柄のcompanyfactsを取得し `xbrl_facts` へ upsert する。

    戻り値は {"tickers": n, "facts_upserted": n, "skipped_no_cik": n, "failures": n}。
    """
    settings = get_settings()
    config = edgar_config or load_edgar_config()
    if not config.enabled:
        return {"tickers": 0, "facts_upserted": 0, "skipped_no_cik": 0, "failures": 0}

    client = EdgarClient(config, settings.edgar_user_agent or "")
    counts = {"tickers": 0, "facts_upserted": 0, "skipped_no_cik": 0, "failures": 0}

    with session_scope() as session:
        if symbols:
            from autoscreener.db.models import Ticker

            tickers = session.query(Ticker).filter(Ticker.symbol.in_([s.upper() for s in symbols])).all()
        else:
            tickers = select_tracked_tickers(session, limit=config.max_tracked_tickers)

        for ticker in tickers:
            if not ticker.cik:
                counts["skipped_no_cik"] += 1
                continue
            try:
                company_facts = client.fetch_company_facts(ticker.cik)
            except EmptyResponseError:
                counts["tickers"] += 1
                continue
            except CollectionError:
                logger.exception("%s: failed to fetch companyfacts", ticker.symbol)
                counts["failures"] += 1
                continue

            counts["tickers"] += 1
            by_concept = extract_all_concepts(company_facts)
            for concept, facts in by_concept.items():
                for fact in facts:
                    stmt = pg_insert(XbrlFact).values(
                        ticker_id=ticker.id,
                        taxonomy=fact.taxonomy,
                        tag=fact.tag,
                        unit=fact.unit,
                        period_start=fact.period_start,
                        period_end=fact.period_end,
                        value=fact.value,
                        form=fact.form,
                        accession_number=fact.accession_number,
                        filed_date=fact.filed_date,
                        fiscal_year=fact.fiscal_year,
                        fiscal_period=fact.fiscal_period,
                    )
                    # UNIQUE(ticker_id, tag, period_end, form, accession_number)。
                    # 同じ提出を再取得しても重複行が入らない。
                    #
                    # **`rowcount` ではなく `RETURNING` で数える。** psycopg経由の
                    # `INSERT ... ON CONFLICT DO NOTHING` は、競合でスキップされた
                    # 場合でも `CursorResult.rowcount` が実際の影響行数(0)を
                    # 正しく反映しないことを実データで確認した(常に1を返す)。
                    # `RETURNING` は実際に挿入された行だけを返すため、これで数える。
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["ticker_id", "tag", "period_end", "form", "accession_number"]
                    ).returning(XbrlFact.id)
                    result = session.execute(stmt)
                    if result.fetchall():
                        counts["facts_upserted"] += 1

    return counts
