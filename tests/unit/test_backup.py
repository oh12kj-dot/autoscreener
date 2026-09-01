"""DBバックアップの内容検証と世代管理。

`pg_dump` の終了コードだけでは空/破損ダンプを検知できないため、保存前に
サイズとヘッダを検証し、疑わしいダンプの保存を拒否することを確認する。
また、バックアップ容量を抑えるため、直近7日＋週次1件＋月次1件の保持方針を
検証する。
"""

import subprocess
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from autoscreener.batch import backup


def _fake_completed(stdout: bytes) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=b"", returncode=0)


def _valid_dump() -> bytes:
    header = b"-- PostgreSQL database dump\n"
    return header + b"-- filler line\n" * 20_000  # 十分大きく


def _write_backup(directory, day: date):
    path = directory / f"autoscreener_{day.isoformat()}.sql.gz"
    path.write_bytes(b"backup")
    return path


def test_run_backup_rejects_empty_dump(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(b""))

    with pytest.raises(RuntimeError, match="suspiciously small"):
        backup.run_backup()

    assert not list((tmp_path / "backups").glob("*.sql.gz"))


def test_run_backup_rejects_small_dump(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    small = b"-- PostgreSQL database dump\nCREATE TABLE t ();\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(small))

    with pytest.raises(RuntimeError, match="suspiciously small"):
        backup.run_backup()


def test_run_backup_rejects_dump_without_header(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    garbage = b"x" * (backup._MIN_EXPECTED_DUMP_BYTES + 10)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(garbage))

    with pytest.raises(RuntimeError, match="does not look like a valid"):
        backup.run_backup()


def test_run_backup_writes_valid_dump(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed(_valid_dump()))

    out_path = backup.run_backup()

    assert out_path.exists()
    assert out_path.suffix == ".gz"


def test_cleanup_keeps_seven_daily_one_weekly_and_one_monthly(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)

    latest = date(2026, 9, 1)
    for days_ago in range(0, 45):
        _write_backup(backup_dir, latest - timedelta(days=days_ago))

    backup._cleanup_old_backups()

    kept = sorted(path.name for path in backup_dir.glob("autoscreener_*.sql.gz"))
    expected = sorted(
        [f"autoscreener_{(latest - timedelta(days=i)).isoformat()}.sql.gz" for i in range(7)]
        + ["autoscreener_2026-08-25.sql.gz"]  # 日次保持期間より前の最新1件
        + ["autoscreener_2026-07-31.sql.gz"]  # 週次復元点より前の別月の最新1件
    )
    assert kept == expected


def test_cleanup_does_not_delete_unrecognized_manual_file(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)

    latest = date(2026, 9, 1)
    for days_ago in range(0, 20):
        _write_backup(backup_dir, latest - timedelta(days=days_ago))

    manual = backup_dir / "autoscreener_manual_before_migration.sql.gz"
    manual.write_bytes(b"manual")

    backup._cleanup_old_backups()

    assert manual.exists()
