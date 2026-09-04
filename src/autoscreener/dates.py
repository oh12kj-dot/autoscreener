"""日付関連の共通ユーティリティ。

`datetime.date.today()` はOSのローカルタイムゾーンに依存する。DBの
`now()`/`current_date`(本プロジェクトのPostgresは既定でUTC)や
`server_default=func.now()` はUTC基準のため、日本時間(UTC+9)等では
ローカル日付とUTC日付が最大1日ずれる時間帯が生じる。

実際に発生した事例:ローカル日付が2026-08-24に切り替わった直後
(UTC 2026-08-23 16:xx)に `apply_gates()` を実行したところ、
`date.today()`(ローカル)で書き込んだ `universe_snapshots.snapshot_date`
と、`current_date`(UTC)で確認しようとした結果が食い違い、
前日分の古い集計を「最新」と誤認しかけた。

アプリ全体で「今日」を扱う箇所は必ずこの関数を経由し、デプロイ先の
タイムゾーンに依存しない一貫した日付を使うこと(8.4:デプロイ先は
未確定であり、ローカルタイムゾーンへの暗黙の依存は避ける)。
"""

from __future__ import annotations

import datetime

# 週次工程が揃って同じ曜日を指すよう、ここを唯一の定義箇所にする
# (`batch/daily_pipeline.py`のユニバース再取得・XBRL実績値等と、
# `collectors/snapshot_collector.py`のyfinance財務諸表週次化(S-2、
# docs/daily_pipeline_throughput_plan_2026-09-04.md)の両方がここを参照する)。
WEEKLY_REFRESH_WEEKDAY = 0  # Monday(date.weekday()の0始まり)


def utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date()


def business_days_between(start: datetime.date, end: datetime.date) -> int:
    """`start`(排他)から `end`(包含)までの営業日数(Mon–Fri、祝日は数えない)。

    A-1(docs/defect_and_edge_audit_2026-08-28.md D-12)のデータ鮮度ガードで使う。
    `end < start` なら負値を返す(方向を保持する)。祝日カレンダーは持たない
    ——小型株スクリーニングの鮮度判定に半日精度は要らず、祝日で1日甘くなる
    ぶんは `max_price_staleness_days` の余裕で吸収する。
    """
    if end == start:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if sign == 1 else (end, start)
    days = 0
    cursor = lo
    while cursor < hi:
        cursor += datetime.timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return sign * days
