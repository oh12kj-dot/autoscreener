"""DBバックアップの内容検証(E-7、defect_audit_2026-08-27.md)。

`pg_dump` の終了コードだけでは空/破損ダンプを検知できないため、保存前に
サイズとヘッダを検証し、疑わしいダンプの保存を拒否することを確認する。
"""

import subprocess
from types import SimpleNamespace

import pytest

from autoscreener.batch import backup


def _fake_completed(stdout: bytes) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=b"", returncode=0)


def _valid_dump() -> bytes:
    header = b"-- PostgreSQL database dump\n"
    return header + b"-- filler line\n" * 20_000  # 十分大きく


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
