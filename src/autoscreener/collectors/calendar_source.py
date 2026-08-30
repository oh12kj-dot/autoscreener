"""次回決算日など「これから起きるイベント」の収集(J-6、investment_decision_gap_2026-08-29.md)。

**ポイントインタイム安全性が最重要。** `earnings_dates` の収集は 27.16 で
**意図的に止めた**——現在時点のスナップショットしか取れず過去に遡れないため、
使うとモデルが検証不能になる。復活させるにあたり、次を守る:

- 保存先は `scores` / `raw_snapshots` から分離した専用テーブル `event_calendar`
- `collected_on`(いつ知った予定か)を必ず持たせる
- `scoring/` と `backtest/` からこのモジュールを **import しないことをテストで固定**
  (`tests/unit/test_event_calendar.py::test_scoring_and_backtest_never_import_calendar`)

yfinance の `Ticker.calendar` は「次回決算日」だけを採る。過去日は捨てる。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

_EARNINGS_KEYS = ("Earnings Date", "earningsDate", "Earnings High", "Earnings Low")


def _coerce_date(value: object) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _iter_candidate_dates(calendar: dict) -> Iterable[datetime.date]:
    for key in _EARNINGS_KEYS:
        if key not in calendar:
            continue
        raw = calendar[key]
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for v in values:
            parsed = _coerce_date(v)
            if parsed is not None:
                yield parsed


def parse_next_earnings_date(
    calendar: dict | None, today: datetime.date
) -> datetime.date | None:
    """yfinance `Ticker.calendar` から、`today` 以降で最も近い決算予定日を返す。

    空・過去日のみ・キー欠損の場合は None(このケースでは行を作らない)。
    """
    if not calendar:
        return None
    future = sorted({d for d in _iter_candidate_dates(calendar) if d >= today})
    return future[0] if future else None


def fetch_calendar(symbol: str) -> dict | None:
    """yfinance から `Ticker.calendar` を best-effort で取得する。

    失敗は None を返す(バッチは1銘柄の失敗では止めない)。DataFrame を返す
    旧バージョンにも備えて dict へ正規化する。
    """
    try:
        import yfinance as yf

        calendar = yf.Ticker(symbol).calendar
    except Exception as exc:  # noqa: BLE001
        logger.debug("failed to fetch calendar for %s: %s", symbol, exc)
        return None
    if calendar is None:
        return None
    if isinstance(calendar, dict):
        return calendar
    # 旧 yfinance は DataFrame(index=項目名, columns=Value...)を返す
    try:
        return {str(k): v for k, v in calendar.to_dict().items()}
    except Exception:  # noqa: BLE001
        return None
