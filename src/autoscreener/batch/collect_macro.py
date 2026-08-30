"""FREDマクロ系列の収集バッチ(30.8.2)。週次(月曜)実行を想定。

**キーが無い場合はこの機能を無効にする**(30.8.1)。フェーズ7が無くても他は
全部動く——`FRED_API_KEY` 未設定は運用上の正常状態でありうる。
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert

from autoscreener.collectors.fred_client import FredClient
from autoscreener.config import FredConfig, get_settings, load_fred_config
from autoscreener.db.models import MacroSeries
from autoscreener.db.session import session_scope

logger = logging.getLogger(__name__)


def collect_macro(*, fred_config: FredConfig | None = None) -> dict[str, int]:
    """3系列を取得し `macro_series` へ upsert する。

    戻り値は {"series": n, "observations_upserted": n}。
    """
    settings = get_settings()
    config = fred_config or load_fred_config()
    if not config.enabled or not settings.fred_api_key:
        logger.info("FRED collection disabled (config.enabled=%s, api_key set=%s)", config.enabled, bool(settings.fred_api_key))
        return {"series": 0, "observations_upserted": 0}

    client = FredClient(settings.fred_api_key)
    counts = {"series": 0, "observations_upserted": 0}

    with session_scope() as session:
        for series_id in config.series_ids:
            try:
                observations = client.fetch_series(series_id)
            except Exception:
                logger.exception("%s: failed to fetch FRED series", series_id)
                continue
            counts["series"] += 1
            for obs in observations:
                if obs.value is None:
                    continue
                # UNIQUE(series_id, observation_date) への upsert。同じ観測日を
                # 再取得しても重複行が入らない(30.8.4の受け入れ基準)。
                stmt = pg_insert(MacroSeries).values(
                    series_id=series_id, observation_date=obs.observation_date, value=obs.value
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["series_id", "observation_date"],
                    set_={"value": stmt.excluded.value},
                )
                session.execute(stmt)
                counts["observations_upserted"] += 1

    return counts
