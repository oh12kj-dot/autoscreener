"""DBバックアップ(18.4)。

`scores`・`forward_returns` は再生成不可能な検証資産(14.3)であり、
他のテーブルより優先度を上げてバックアップする対象。DB全体をpg_dumpし、
圧縮して保存する(個人利用規模を想定した簡易実装。8.4)。

保持ポリシーは容量と復元性のバランスを取り、直近7日の日次バックアップに加えて、
それより古い週次復元点を1件、さらに古い月次復元点を1件だけ残す。
日次実行が連続している場合、通常は最大9ファイルに収まる。
"""

from __future__ import annotations

import gzip
import logging
import subprocess
from datetime import date, timedelta
from pathlib import Path

from autoscreener.dates import utc_today

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKUP_DIR = _PROJECT_ROOT / "backups"
DAILY_RETENTION_DAYS = 7

# E-7(2026-08-27、docs/defect_audit_2026-08-27.md):`check=True` は pg_dump の終了
# コードしか見ないため、空/破損ダンプ(DBコンテナ未起動で接続だけ成立、権限
# エラーがstderrに出つつ終了コード0 等)がそのまま「成功」として保存され、
# ローテーションが正常だった古いバックアップを消していく。最低限の内容検証を
# 行い、疑わしいダンプは保存を拒否する。
#
# 直近の実バックアップ(gzip前の生SQL)のサイズを `backups/` で確認し、その
# 1/10 程度に余裕を持たせた下限。スキーマ+初期データだけでもこれは超える。
_MIN_EXPECTED_DUMP_BYTES = 100_000
_DUMP_HEADER_MARKER = b"-- PostgreSQL database dump"


def run_backup() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    out_path = BACKUP_DIR / f"autoscreener_{utc_today().isoformat()}.sql.gz"

    dump = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "autoscreener", "autoscreener"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        check=True,
    )
    # E-7: 終了コードだけでは空/破損ダンプを検知できない。保存前に内容を検証する。
    if len(dump.stdout) < _MIN_EXPECTED_DUMP_BYTES:
        raise RuntimeError(
            f"pg_dump output suspiciously small ({len(dump.stdout)} bytes, "
            f"expected >= {_MIN_EXPECTED_DUMP_BYTES}) — refusing to save as backup (E-7)"
        )
    if _DUMP_HEADER_MARKER not in dump.stdout[:1000]:
        raise RuntimeError(
            "pg_dump output does not look like a valid PostgreSQL dump — refusing to save as backup (E-7)"
        )

    with gzip.open(out_path, "wb") as f:
        f.write(dump.stdout)

    logger.info("backup written: %s (%d bytes)", out_path, out_path.stat().st_size)
    _cleanup_old_backups()
    return out_path


def _backup_date(path: Path) -> date | None:
    """管理対象のバックアップ名から日付を返す。

    手動退避など命名規則外のファイルは安全のためローテーション対象にしない。
    """

    prefix = "autoscreener_"
    suffix = ".sql.gz"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    value = name[len(prefix) : -len(suffix)]
    try:
        return date.fromisoformat(value)
    except ValueError:
        logger.warning("ignoring backup with unrecognized date: %s", path)
        return None


def _cleanup_old_backups() -> None:
    dated_backups: list[tuple[date, Path]] = []
    for path in BACKUP_DIR.glob("autoscreener_*.sql.gz"):
        backup_date = _backup_date(path)
        if backup_date is not None:
            dated_backups.append((backup_date, path))

    if not dated_backups:
        return

    dated_backups.sort(key=lambda item: item[0])
    latest_date = dated_backups[-1][0]
    daily_cutoff = latest_date - timedelta(days=DAILY_RETENTION_DAYS - 1)

    # 1) 最新日を含む直近7日分はすべて保持する。
    keep: set[Path] = {path for backup_date, path in dated_backups if backup_date >= daily_cutoff}

    # 2) 日次保持期間より前の最新1件を週次復元点として保持する。
    older_than_daily = [item for item in dated_backups if item[0] < daily_cutoff]
    weekly_date: date | None = None
    if older_than_daily:
        weekly_date, weekly_path = older_than_daily[-1]
        keep.add(weekly_path)

    # 3) 週次復元点よりさらに古い「別の月」から最新1件を月次復元点として保持する。
    #    同じ月の8日目・9日目を残すだけにならないよう、月境界を明示する。
    if weekly_date is not None:
        weekly_month_start = weekly_date.replace(day=1)
        older_months = [item for item in dated_backups if item[0] < weekly_month_start]
        if older_months:
            _, monthly_path = older_months[-1]
            keep.add(monthly_path)

    for _, old in dated_backups:
        if old not in keep:
            old.unlink()
            logger.info("removed old backup: %s", old)
