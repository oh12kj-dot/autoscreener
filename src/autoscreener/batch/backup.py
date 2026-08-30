"""DBバックアップ(18.4)。

`scores`・`forward_returns` は再生成不可能な検証資産(14.3)であり、
他のテーブルより優先度を上げてバックアップする対象。DB全体をpg_dumpし、
圧縮して保存する(個人利用規模を想定した簡易実装。8.4)。
"""

from __future__ import annotations

import gzip
import logging
import subprocess
from pathlib import Path

from autoscreener.dates import utc_today

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKUP_DIR = _PROJECT_ROOT / "backups"
RETENTION_COUNT = 14  # 直近14日分を保持(個人利用規模のため簡易なローテーション)

# E-7(2026-08-27、defect_audit_2026-08-27.md):`check=True` は pg_dump の終了
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


def _cleanup_old_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("autoscreener_*.sql.gz"))
    for old in backups[:-RETENTION_COUNT]:
        old.unlink()
        logger.info("removed old backup: %s", old)
