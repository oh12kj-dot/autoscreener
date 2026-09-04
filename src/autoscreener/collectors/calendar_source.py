"""次回決算日など「これから起きるイベント」の収集(J-6、docs/investment_decision_gap_2026-08-29.md)。

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

# 2026-09-04監査(daily_pipeline_throughput_plan_2026-09-04.md、S-1/S-4の
# 同型欠陥のスイープで発見):この`import`は見た目上未使用に見えるが、
# 副作用が目的である。`_install_http_throttle()`(S-1)は
# `collectors/yfinance_client`が**importされた時点で**`yfinance.data.
# YfData._make_request`をモンキーパッチする——プロセス起動時に自動で
# 走る保証ではなく、このモジュールの側から明示的にimportして初めて効く。
# 以前はここで`fetch_calendar()`が独立に`import yfinance as yf`していた
# ため、`collectors.calendar_source`(延いては週次`events`工程、
# `batch/collect_events.py`)だけを単体でimportする経路ではスロットルが
# 一切入らず、`run-daily-pipeline`で偶然先に`yfinance_client`が
# importされていたから安全に見えていただけだった(`collectors/consensus.py`
# で見つかったのと同じ欠陥の別インスタンス)。
from autoscreener.collectors import yfinance_client as _yfinance_client

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
        # `_yfinance_client.yf`は`collectors/yfinance_client`が既に
        # `import yfinance as yf`した参照の再利用(モジュール先頭のコメント
        # 参照)。ここで独立に`import yfinance as yf`しないのは、そちらの
        # 経路だと「モジュールをimportしただけではスロットルが入る保証が
        # ない」という元の欠陥に戻ってしまうため。
        calendar = _yfinance_client.yf.Ticker(symbol).calendar
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
