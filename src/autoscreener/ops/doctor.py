"""`doctor` 診断コマンドの中身(K-9、自動化計画2026-08-30)。

**背景(なぜこれが要るか)**:このアプリの日次パイプライン(`batch/daily_pipeline.py`)は
EDGAR/FRED連携の各工程を意図的に `try/except` で握り潰して先へ進む設計になっている
(工程が1つ落ちても当日のスコア計算自体は無駄にしない、という判断)。その代償として、
`.env` に `EDGAR_USER_AGENT` と `FRED_API_KEY` が無いまま数週間動き続け、
`filings` / `xbrl_facts` / `macro_series` / `tickers.delisted_at` が全部0行のまま誰も
気づかなかった、という実際の障害が起きた。エラーは表に出ず、機能だけが空で回っていた。

つまり「人間が異常に気づく」こと自体が、いま人間に残っている運用作業である。
このモジュールはそれを機械にやらせる——設定・DB接続・データの空/鮮度・隔離率・
直近のパイプライン実行を一度に診断し、**直し方まで**出す。

**設計方針**:
1. `monitoring.py` の `HealthFinding` / `check_quarantine_health` が持つ語彙
   (severity は "warning"/"error"、既存の閾値・判定ロジック)をそのまま再利用する。
   新しい健全性の概念を並行して作らない。
2. 判定ロジックはすべて「行数・最終日付・環境変数の有無」のような素の値を引数に取る
   純関数として書く。DB接続・ファイル読み込みなど副作用を持つのは `run_doctor()` と
   その直下のプライベート関数だけ。これによりテストがDBにもネットワークにも
   触らずに判定ロジックだけを検証できる。
3. 各所見は必ず `remedy`(直すための具体的なコマンド1行)を持つ。「異常です」とだけ
   言って直し方を出さない診断は、人間の作業を増やすだけで減らさない。
4. 環境変数の**値そのもの**は所見のどのフィールドにも絶対に含めない
   (`DATABASE_URL` はパスワードを含みうる文字列であり、ログ・標準出力に
   流れる前提のオブジェクトに生の接続文字列を混ぜるのは秘密の漏洩そのもの)。
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from autoscreener.config import PROJECT_ROOT
from autoscreener.dates import utc_today
from autoscreener.db.models import (
    EventCalendar,
    Filing,
    ForwardReturn,
    InsiderTransaction,
    MacroSeries,
    PipelineRun,
    PriceSnapshot,
    RawSnapshot,
    Score,
    ShortInterest,
    Ticker,
    UniverseSnapshot,
    XbrlFact,
)
from autoscreener.db.session import get_engine, session_scope
from autoscreener.monitoring import HealthFinding, check_quarantine_health

# --- 所見の型 -----------------------------------------------------------------


@dataclass(frozen=True)
class DoctorFinding:
    """`doctor` 1件分の所見。

    `monitoring.HealthFinding` に `remedy` を足しただけの派生形として意図的に
    別クラスにしてある。`HealthFinding` は `daily_pipeline.py` / `pipeline_recorder.py`
    / フロントエンド(`frontend/src/pipelineHealth.ts`)から4フィールド固定の契約として
    参照されているため、直接フィールドを増やして壊す変更はしない
    (`code`/`severity`/`message`/`detail` の語彙自体は完全に踏襲する)。
    """

    code: str
    severity: str  # "warning" / "error"(monitoring.HealthFindingと同じ語彙)
    message: str
    detail: dict
    remedy: str


@dataclass(frozen=True)
class DoctorReport:
    """`run_doctor()` の戻り値。`ok=False` なら CLI 側が終了コード1を返す想定
    (`sys.exit` の判断自体はCLI側に委ねる。このモジュールはプロセスを終了させない)。
    """

    ok: bool
    findings: list[DoctorFinding] = field(default_factory=list)
    checked_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


# --- 1. 設定(.env必須キー) -----------------------------------------------------

# キー -> (欠けると何が起きるかの説明, 直し方)。
# **`config.Settings.database_url` はコード側に既定値を持つため、
# `Settings()` 経由では「未設定」と「既定値と偶然一致」を区別できない。**
# そのため doctor は `.env` ファイルとOS環境変数を直接読む(`_read_env_values`)。
REQUIRED_ENV_KEYS: dict[str, tuple[str, str]] = {
    "DATABASE_URL": (
        "アプリ本体のDB接続文字列。未設定のままだとコード内蔵の既定値"
        "(ローカルDocker想定の弱い認証情報)にフォールバックし、"
        "意図しないDBに静かに繋がり続ける恐れがあります。",
        '.env に DATABASE_URL="postgresql+psycopg://<user>:<pass>@<host>:5432/<db>" を設定してください。',
    ),
    "API_DATABASE_URL": (
        "APIレイヤー用の読み取り専用DB接続文字列(18.6)。未設定でも "
        "DATABASE_URL にフォールバックして動作は止まりませんが、書き込み権限を"
        "持つロールでAPIを動かし続けることになります。",
        ".env に API_DATABASE_URL を設定してください"
        "(先に scripts/create_readonly_role.sql で読み取り専用ロールを作成)。",
    ),
    "EDGAR_USER_AGENT": (
        "SEC EDGARが要求する連絡先つきUser-Agent(30.3.1)。未設定のままだと "
        "filings / xbrl_facts / tickers.delisted_at(collect-delistings) / "
        "insider_transactions に関わるEDGARバッチがすべて ValueError で落ち、"
        "daily_pipeline はログに残すだけで先へ進みます(握り潰し)"
        "——これが今回の障害の直接原因です。",
        '.env に EDGAR_USER_AGENT="<アプリ名> <連絡先メールアドレス>" を設定してください。',
    ),
    "FRED_API_KEY": (
        "FRED(セントルイス連銀)APIキー(30.8.1)。未設定のままだと "
        "macro_series がいつまでも0行のままになります"
        "(collect_macro が例外を送出し、daily_pipeline はログに残すだけで先へ進みます)。",
        ".env に FRED_API_KEY を設定してください"
        "(https://fred.stlouisfed.org/docs/api/api_key.html で無料発行)。",
    ),
}


def check_env_keys(values: dict[str, str | None]) -> list[DoctorFinding]:
    """`.env` の必須キーが埋まっているかを判定する(純関数)。

    **`values` に入っている実際の値は所見のどのフィールドにも絶対に使わない**
    ——埋まっているか否かの真偽値だけを見る。欠けている場合は、そのキーが
    欠けると空のままになるテーブル名を本文(`message`)に書く。これが
    今回の障害の再発防止の本体である。
    """
    findings: list[DoctorFinding] = []
    for key, (impact, remedy) in REQUIRED_ENV_KEYS.items():
        value = values.get(key)
        if value is None or not value.strip():
            findings.append(
                DoctorFinding(
                    code="env_key_missing",
                    severity="error",
                    message=f"{key} が未設定です。{impact}",
                    detail={"key": key},
                    remedy=remedy,
                )
            )
    return findings


# --- 2. DB接続・マイグレーション ------------------------------------------------


def check_db_connection(error: BaseException | None) -> list[DoctorFinding]:
    """DB接続確認の結果を所見にする(純関数)。

    **例外メッセージ(`str(error)`)は使わない。** psycopg/SQLAlchemyの接続エラー
    メッセージには、接続に使ったDSN(パスワードを含みうる)がそのまま含まれる
    ことがある。例外クラス名だけを出すことで、接続失敗の性質は伝えつつ
    秘密の漏洩を避ける。
    """
    if error is None:
        return []
    return [
        DoctorFinding(
            code="db_connection_failed",
            severity="error",
            message=f"DBに接続できません({type(error).__name__})。",
            detail={"error_type": type(error).__name__},
            remedy="Postgresが起動しているか確認してください: docker compose up -d --wait",
        )
    ]


def check_alembic_revision(current_rev: str | None, head_rev: str | None) -> list[DoctorFinding]:
    """DBの `alembic_version` が最新リビジョン(head)と一致しているかを判定する(純関数)。"""
    if current_rev == head_rev:
        return []
    return [
        DoctorFinding(
            code="alembic_not_at_head",
            severity="error",
            message=(
                f"DBスキーマが最新のマイグレーションに追随していません"
                f"(current={current_rev!r}, head={head_rev!r})。"
            ),
            detail={"current_revision": current_rev, "head_revision": head_rev},
            remedy="uv run alembic upgrade head を実行してください。",
        )
    ]


# --- 3. テーブルの空検知 ---------------------------------------------------------

# 0行それ自体を一律にエラーにしないための3分類。各テーブルの根拠は下記コメント。
#
# (1) 日次パイプラインが最低1回でも走っていれば0行はまず有り得ない中核テーブル。
#     tickers/universe_snapshots は collect-universe で、raw_snapshots/
#     price_snapshots/scores は run-daily-pipeline の中核工程
#     (collection/gates/scoring — monitoring.CORE_STAGES と同じ顔ぶれ)で埋まる。
ZERO_ALWAYS_ANOMALY_TABLES: frozenset[str] = frozenset(
    {"tickers", "universe_snapshots", "raw_snapshots", "price_snapshots", "scores"}
)

# (2) 対応する環境変数キーが未設定なら0行は正常(機能自体が無効化されている)。
#     設定済みなのに0行なら、EDGAR接続やCIKマッピング未突合など別の要因を
#     疑うべきサインなのでエラーにする——これがまさに今回の障害
#     (EDGAR_USER_AGENT/FRED_API_KEY未設定のまま filings/xbrl_facts/macro_series
#     が0行のまま誰も気づかなかった件)と同じ形の、静かな失敗の再発防止。
ZERO_CONDITIONAL_ON_ENV: dict[str, str] = {
    "filings": "EDGAR_USER_AGENT",
    "xbrl_facts": "EDGAR_USER_AGENT",
    "macro_series": "FRED_API_KEY",
}

# (3) 条件に関わらず0行が正常でありうるテーブル。
#     - forward_returns: 最短ホライズン(1M)の期日がまだ来ていなければ0が正常
#       (monitoring.py 冒頭のコメントと同じ判断。ここでは新しい理屈を作らない)。
#     - event_calendar: J-6、追跡対象銘柄限定のベストエフォート週次収集。
#       追跡対象が少ない・決算日がyfinanceから取れない銘柄ばかりでも0になりうる。
#     - insider_transactions/short_interest: J-7。`batch/collect_supply.py` の
#       docstringが明記する通り、実際のEDGAR/FINRA取得経路(`*_fetcher`)は
#       まだ配線されておらず既定fetcherは空を返す設計——EDGAR_USER_AGENTの
#       有無に関係なく現状は常に0が正常。配線後はこの2テーブルを(2)へ移すこと。
ZERO_NORMAL_TABLES: frozenset[str] = frozenset(
    {"forward_returns", "event_calendar", "insider_transactions", "short_interest"}
)

_CORE_TABLE_REMEDY: dict[str, str] = {
    "tickers": "uv run python -m autoscreener.cli collect-universe を実行してください。",
    "universe_snapshots": "uv run python -m autoscreener.cli collect-universe を実行してください"
    "(ユニバース再取得と同時に書かれます)。",
    "raw_snapshots": "uv run python -m autoscreener.cli run-daily-pipeline を実行してください(収集工程)。",
    "price_snapshots": "uv run python -m autoscreener.cli run-daily-pipeline を実行してください(収集工程)。",
    "scores": "uv run python -m autoscreener.cli run-scoring を実行してください"
    "(先に collect/apply-gates が必要です)。",
}

_CONDITIONAL_TABLE_REMEDY: dict[str, str] = {
    "filings": "uv run python -m autoscreener.cli collect-filings を実行し、"
    "ログでEDGAR接続エラーが無いか確認してください。",
    "xbrl_facts": "uv run python -m autoscreener.cli collect-xbrl を実行してください。"
    "CIKが未突合の可能性もあるので refresh-cik-map も確認してください。",
    "macro_series": "uv run python -m autoscreener.cli collect-macro を実行し、"
    "FRED_API_KEY が有効か確認してください。",
}


def check_table_row_counts(
    row_counts: dict[str, int],
    *,
    edgar_enabled: bool,
    fred_enabled: bool,
) -> list[DoctorFinding]:
    """主要テーブルの行数から「空のまま放置」を検知する(純関数)。"""
    findings: list[DoctorFinding] = []

    if row_counts.get("pipeline_runs", 0) == 0:
        findings.append(
            DoctorFinding(
                code="pipeline_never_run",
                severity="error",
                message="pipeline_runs が0件です。日次パイプラインが一度も実行されていません。",
                detail={"table": "pipeline_runs", "count": 0},
                remedy="uv run python -m autoscreener.cli run-daily-pipeline を実行してください"
                "(初回実行)。継続実行の自動化には scripts/register_scheduled_task.ps1 -Apply を"
                "使ってください。",
            )
        )
        # 一度も実行されていないなら、他の中核テーブルが0件なのは当然の帰結であり、
        # 同じ原因を何件も重ねて報告しても情報量が増えない(むしろ「たくさん壊れて
        # いるように見える」というアラート疲れを起こす)。
        return findings

    for table in sorted(ZERO_ALWAYS_ANOMALY_TABLES):
        if row_counts.get(table, 0) == 0:
            findings.append(
                DoctorFinding(
                    code="table_empty",
                    severity="error",
                    message=f"{table} が0行です。日次パイプラインは実行済みなのに空のままなのは異常です。",
                    detail={"table": table, "count": 0},
                    remedy=_CORE_TABLE_REMEDY[table],
                )
            )

    for table, env_key in ZERO_CONDITIONAL_ON_ENV.items():
        enabled = edgar_enabled if env_key == "EDGAR_USER_AGENT" else fred_enabled
        if enabled and row_counts.get(table, 0) == 0:
            findings.append(
                DoctorFinding(
                    code="table_empty_despite_enabled",
                    severity="error",
                    message=(
                        f"{table} が0行です。{env_key} は設定されているのにデータが1件も無いのは、"
                        "今回の障害(EDGAR_USER_AGENT/FRED_API_KEY未設定のまま数週間気づかれなかった"
                        "件)と同じ形の、静かな失敗です。"
                    ),
                    detail={"table": table, "count": 0, "env_key": env_key},
                    remedy=_CONDITIONAL_TABLE_REMEDY[table],
                )
            )

    return findings


# --- 4. 鮮度 ----------------------------------------------------------------

# 鮮度閾値(暦日)。対象は「収集日そのものが日付列になっている」テーブルに限る。
#
# **filings / xbrl_facts / event_calendar / insider_transactions / short_interest /
# forward_returns はここに含めない。** これらの最新日付は実世界のイベント
# (SEC提出日・決算日・ホライズン到来日)に紐づいており、収集が正常でも
# 「しばらく新しい行が無い」ことが普通に起こる(例:10-Qは四半期に1回しか
# 提出されない)。日付の古さだけを見て「壊れている」と誤検知するほうが、
# 見逃すより実害が大きい(アラート疲れで本物の異常も無視されるようになる)。
# これらの稼働状況は (3) の空検知と、直近パイプライン実行の成否で代わりに見る。
#
# 閾値の根拠:
#   - 日次系(raw_snapshots/price_snapshots/scores/pipeline_runs):金曜収集→
#     月曜確認で最大3暦日空く。祝日1日分のスラックを足して4暦日。
#     `scoring/engine.py` の `FreshnessConfig.max_price_staleness_days`(2営業日)
#     を暦日にそのまま流用すると週末で毎週月曜に誤検知するため、doctorは
#     暦日基準の緩い閾値を独自に持つ(営業日カレンダーを持ち込まずに済む)。
#   - 週次系(universe_snapshots/macro_series):月曜1回。祝日で1週ずれても
#     許容できるよう10暦日(1.5週)。
FRESHNESS_THRESHOLDS_DAYS: dict[str, int] = {
    "raw_snapshots": 4,
    "price_snapshots": 4,
    "scores": 4,
    "pipeline_runs": 4,
    "universe_snapshots": 10,
    "macro_series": 10,
}

# macro_series のみ FRED_API_KEY 未設定なら鮮度チェック自体をスキップする
# (機能が無効化されているだけであり、「古い」という診断は意味を持たない)。
_FRESHNESS_CONDITIONAL_ON_ENV: dict[str, str] = {"macro_series": "FRED_API_KEY"}

_FRESHNESS_REMEDY: dict[str, str] = {
    "raw_snapshots": "run-daily-pipeline が最近実行されていません。"
    "scripts/register_scheduled_task.ps1 でタスク登録状況を確認してください。",
    "price_snapshots": "run-daily-pipeline が最近実行されていません。"
    "scripts/register_scheduled_task.ps1 でタスク登録状況を確認してください。",
    "scores": "uv run python -m autoscreener.cli run-scoring が最近実行されていません。"
    "run-daily-pipeline のログを確認してください。",
    "pipeline_runs": "日次パイプラインが最近実行されていません。"
    "scripts/register_scheduled_task.ps1 -Apply でタスクスケジューラ登録を確認してください。",
    "universe_snapshots": "uv run python -m autoscreener.cli collect-universe が最近実行されていません"
    "(週次工程は月曜のみ走ります)。",
    "macro_series": "uv run python -m autoscreener.cli collect-macro が最近成功していません。"
    "FRED_API_KEY が有効か確認してください。",
}


def check_table_freshness(
    latest_dates: dict[str, datetime.date | None],
    today: datetime.date,
    *,
    edgar_enabled: bool,
    fred_enabled: bool,
) -> list[DoctorFinding]:
    """各テーブルの最新日付が想定更新頻度に対して古すぎないかを判定する(純関数)。

    行が1件も無いテーブル(`latest_dates[table] is None`)は
    `check_table_row_counts` 側が既に報告しているので、ここでは対象外にする
    (同じ原因を2つの所見で二重に報告しない)。
    """
    findings: list[DoctorFinding] = []
    for table, threshold_days in FRESHNESS_THRESHOLDS_DAYS.items():
        env_key = _FRESHNESS_CONDITIONAL_ON_ENV.get(table)
        if env_key is not None:
            enabled = edgar_enabled if env_key == "EDGAR_USER_AGENT" else fred_enabled
            if not enabled:
                continue

        latest = latest_dates.get(table)
        if latest is None:
            continue

        age_days = (today - latest).days
        if age_days > threshold_days:
            findings.append(
                DoctorFinding(
                    code="table_stale",
                    severity="warning",
                    message=(
                        f"{table} の最新日付が{age_days}日前({latest.isoformat()})です"
                        f"(閾値{threshold_days}日)。"
                    ),
                    detail={
                        "table": table,
                        "latest_date": latest.isoformat(),
                        "age_days": age_days,
                        "threshold_days": threshold_days,
                    },
                    remedy=_FRESHNESS_REMEDY[table],
                )
            )
    return findings


# --- 5. 隔離率(既存 check_quarantine_health の再利用) ---------------------------

_QUARANTINE_REMEDY = (
    "uv run python -m autoscreener.cli recover-quarantine --help で隔離解除コマンドの"
    "使い方を確認するか、次回収集の再挑戦期限(config/collection.yaml の quarantine 設定)"
    "を待ってください。"
)


def wrap_quarantine_findings(findings: Sequence[HealthFinding]) -> list[DoctorFinding]:
    """`monitoring.check_quarantine_health` の結果に remedy を添えて包み直す(純関数)。

    判定ロジック・閾値は一切変えない——「既存の型と語彙を再利用する」方針の実体。
    """
    return [
        DoctorFinding(
            code=f.code, severity=f.severity, message=f.message, detail=f.detail, remedy=_QUARANTINE_REMEDY
        )
        for f in findings
    ]


# --- 6. 直近のパイプライン実行 ---------------------------------------------------

# daily_job_status_screen_2026-08-30.md §4.3 と同じ閾値・同じ理由
# (8.1で想定する所要時間の十分な上振れ)。
ORPHAN_THRESHOLD = datetime.timedelta(hours=6)

_HEALTH_CODE_REMEDY: dict[str, str] = {
    "collection_success_rate_low": "logs/daily_pipeline_YYYYMMDD.log でcollection工程のエラー内容を"
    "確認してください(yfinance側のレート制限・障害であることが多いです)。",
    "sanitized_ratio_elevated": "raw_snapshots.validation_errors を確認し、"
    "どのフィールドが無効化されているか調査してください。",
    "quarantine_ratio_high": _QUARANTINE_REMEDY,
    "collection_target_empty": "ユニバース全銘柄が隔離されている可能性があります。"
    "uv run python -m autoscreener.cli recover-quarantine を検討してください。",
    "scoring_skipped": "logs/daily_pipeline_YYYYMMDD.log でscoring工程のskipped_reasonを"
    "確認してください(価格鮮度不足が典型です)。",
    "scoring_yield_dropped": "pipeline_stage_runs.result で collection/gates 工程の件数を確認してください。",
    "stage_failed": "pipeline_stage_runs.error_traceback 列で該当工程の例外内容を確認してください。",
}
_DEFAULT_HEALTH_CODE_REMEDY = "logs/daily_pipeline_YYYYMMDD.log と pipeline_stage_runs テーブルで詳細を確認してください。"


def check_last_pipeline_run(
    *,
    run_date: datetime.date | None,
    status: str | None,
    started_at: datetime.datetime | None,
    finished_at: datetime.datetime | None,
    health: list[dict] | None,
    now: datetime.datetime,
) -> list[DoctorFinding]:
    """直近の `pipeline_runs` 1件を診断する(純関数)。

    `run_date is None`(実行履歴が無い)場合は `check_table_row_counts` の
    `pipeline_never_run` が既に報告しているので、ここでは何も返さない。

    `pipeline_runs.health` には `daily_pipeline.py` が実行時点で記録した
    `HealthFinding` 相当のJSONがそのまま入っている——doctorはこれを
    再計算せず、そのままremedyを添えて表面化するだけにする(§3.4の判定
    ロジックを二重実装しない)。
    """
    if run_date is None:
        return []

    findings: list[DoctorFinding] = []

    # §4.3:孤児実行。プロセスが死んで finished_at が永遠にNULLのまま残る。
    if finished_at is None and started_at is not None and now - started_at > ORPHAN_THRESHOLD:
        findings.append(
            DoctorFinding(
                code="run_orphaned",
                severity="error",
                message=(
                    f"直近の実行(run_date={run_date.isoformat()})が"
                    f"{ORPHAN_THRESHOLD.total_seconds() / 3600:.0f}時間以上 finished_at 無しのままです"
                    "(プロセスが強制終了した可能性)。"
                ),
                detail={"run_date": run_date.isoformat(), "started_at": started_at.isoformat()},
                remedy="logs/daily_pipeline_YYYYMMDD.log を確認し、再実行してください: "
                "uv run python -m autoscreener.cli run-daily-pipeline",
            )
        )
    elif status == "failed":
        findings.append(
            DoctorFinding(
                code="run_failed",
                severity="error",
                message=f"直近の実行(run_date={run_date.isoformat()})が failed で終わっています。",
                detail={"run_date": run_date.isoformat(), "status": status},
                remedy="logs/daily_pipeline_YYYYMMDD.log と pipeline_stage_runs.error_traceback で"
                "失敗工程を確認してください。",
            )
        )

    for item in health or []:
        code = item.get("code", "")
        findings.append(
            DoctorFinding(
                code=code,
                severity=item.get("severity", "warning"),
                message=item.get("message", ""),
                detail=item.get("detail") or {},
                remedy=_HEALTH_CODE_REMEDY.get(code, _DEFAULT_HEALTH_CODE_REMEDY),
            )
        )

    return findings


# --- オーケストレーション(ここだけがDB/ファイルに触る) ----------------------------

_TABLE_MODELS: dict[str, type] = {
    "tickers": Ticker,
    "raw_snapshots": RawSnapshot,
    "price_snapshots": PriceSnapshot,
    "scores": Score,
    "universe_snapshots": UniverseSnapshot,
    "filings": Filing,
    "xbrl_facts": XbrlFact,
    "macro_series": MacroSeries,
    "event_calendar": EventCalendar,
    "insider_transactions": InsiderTransaction,
    "short_interest": ShortInterest,
    "forward_returns": ForwardReturn,
    "pipeline_runs": PipelineRun,
}

# 鮮度チェック対象テーブルの日付列名(FRESHNESS_THRESHOLDS_DAYSと1対1対応)。
_TABLE_DATE_COLUMNS: dict[str, str] = {
    "raw_snapshots": "snapshot_date",
    "price_snapshots": "trade_date",
    "scores": "score_date",
    "universe_snapshots": "snapshot_date",
    "macro_series": "observation_date",
    "pipeline_runs": "run_date",
}


def _read_env_values() -> dict[str, str | None]:
    """`.env` ファイルとOS環境変数から4キーの生の値を集める(唯一の副作用箇所)。

    `config.Settings`(pydantic-settings)を経由しないのは、`database_url` が
    コード側の既定値を持つため「未設定」と「既定値と偶然一致」を区別できないから。
    `dotenv_values` で `.env` を直接読み、OS環境変数で上書きする——これは
    pydantic-settings 自身の解決順序(OS env が `.env` より優先)と同じにしてある。
    """
    from dotenv import dotenv_values

    file_values = dotenv_values(str(PROJECT_ROOT / ".env"))
    merged: dict[str, str | None] = dict(file_values)
    for key in REQUIRED_ENV_KEYS:
        if key in os.environ:
            merged[key] = os.environ[key]
    return {key: merged.get(key) for key in REQUIRED_ENV_KEYS}


def _check_db_and_alembic() -> tuple[list[DoctorFinding], bool]:
    """DB接続とalembicリビジョンを確認する。

    戻り値の2つ目はDBに接続できたかどうか——`False` なら以降のテーブル系
    チェック(行数・鮮度・隔離率・直近実行)は原理的に実行できないため、
    呼び出し側(`run_doctor`)はそれらをスキップする。
    """
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    engine = get_engine()
    try:
        with engine.connect() as conn:
            current_rev = MigrationContext.configure(conn).get_current_revision()
    except Exception as exc:  # noqa: BLE001 — 接続失敗の種類を問わず同じ扱いにする
        return check_db_connection(exc), False

    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    head_rev = ScriptDirectory.from_config(alembic_cfg).get_current_head()

    return check_alembic_revision(current_rev, head_rev), True


def _gather_table_stats(session: Session) -> tuple[dict[str, int], dict[str, datetime.date | None]]:
    row_counts = {name: session.query(model).count() for name, model in _TABLE_MODELS.items()}
    latest_dates: dict[str, datetime.date | None] = {}
    for table, column_name in _TABLE_DATE_COLUMNS.items():
        column = getattr(_TABLE_MODELS[table], column_name)
        latest_dates[table] = session.query(func.max(column)).scalar()
    return row_counts, latest_dates


def run_doctor() -> DoctorReport:
    """設定・DB接続・データ鮮度・隔離率・直近実行を一度に診断する(K-9)。

    ここは薄いオーケストレーションだけを持つ——判定ロジックは全て上の純関数に
    切り出してあり、ここでは「何を読んでどの純関数に渡すか」だけを担う。
    """
    findings: list[DoctorFinding] = []

    env_values = _read_env_values()
    findings.extend(check_env_keys(env_values))
    edgar_enabled = bool((env_values.get("EDGAR_USER_AGENT") or "").strip())
    fred_enabled = bool((env_values.get("FRED_API_KEY") or "").strip())

    db_findings, db_ok = _check_db_and_alembic()
    findings.extend(db_findings)

    if db_ok:
        with session_scope() as session:
            row_counts, latest_dates = _gather_table_stats(session)
            quarantined_count = session.query(Ticker).filter(Ticker.is_quarantined.is_(True)).count()
            latest_run = (
                session.query(PipelineRun)
                .order_by(PipelineRun.run_date.desc(), PipelineRun.started_at.desc())
                .first()
            )

        today = utc_today()
        findings.extend(
            check_table_row_counts(row_counts, edgar_enabled=edgar_enabled, fred_enabled=fred_enabled)
        )
        findings.extend(
            check_table_freshness(latest_dates, today, edgar_enabled=edgar_enabled, fred_enabled=fred_enabled)
        )
        findings.extend(wrap_quarantine_findings(check_quarantine_health(quarantined_count, row_counts.get("tickers", 0))))
        findings.extend(
            check_last_pipeline_run(
                run_date=latest_run.run_date if latest_run else None,
                status=latest_run.status if latest_run else None,
                started_at=latest_run.started_at if latest_run else None,
                finished_at=latest_run.finished_at if latest_run else None,
                health=latest_run.health if latest_run else None,
                now=datetime.datetime.now(datetime.UTC),
            )
        )

    ok = not any(f.severity == "error" for f in findings)
    return DoctorReport(ok=ok, findings=findings)


def format_doctor_report(report: DoctorReport) -> str:
    """人間可読のテキストに整形する(`cli.py` の `reconcile` コマンドの表整形に合わせる)。"""
    lines: list[str] = []
    status = "OK" if report.ok else "NG"
    lines.append(f"doctor診断結果: {status}({len(report.findings)}件の所見、{report.checked_at.isoformat()}時点)")
    if not report.findings:
        lines.append("  所見なし。")
        return "\n".join(lines)

    severity_order = {"error": 0, "warning": 1}
    for finding in sorted(report.findings, key=lambda f: severity_order.get(f.severity, 2)):
        label = "ERROR" if finding.severity == "error" else "WARN "
        lines.append(f"[{label}] {finding.code}: {finding.message}")
        lines.append(f"        remedy: {finding.remedy}")
    return "\n".join(lines)
