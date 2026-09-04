"""A-1の回帰テスト(docs/racr_wp_a_operational_safety_2026-09-04.md)。

`tests/conftest.py` のDB隔離ガード(`_require_isolated_test_database`)が、
**collectionが始まる前に** `TEST_DATABASE_URL` 未設定・production相当DB名の
両方を確実に落とすことを確認する。

このテスト自身はサブプロセスとして `python -m pytest` をもう一段起動する。
`pytest_configure` はプロセス起動のたびに1回しか評価されないので、同一
プロセス内で環境変数を差し替えて再現することはできない。ターゲットには
使い捨ての `tests/unit/support/dummy_ok.py`(通常の `pytest tests/` 実行では
収集されないファイル)を渡す——ガードが万一壊れて素通りした場合でも、
再帰的に自分自身を呼び出すのではなく trivial なテストが1件走るだけにする
ため。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DUMMY_TARGET = "tests/unit/support/dummy_ok.py"


def _run_pytest_subprocess(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, "-m", "pytest", _DUMMY_TARGET, "-q"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_missing_test_database_url_fails_before_collection() -> None:
    """`TEST_DATABASE_URL` 未設定なら、collectionへ進む前に非0終了する。"""
    result = _run_pytest_subprocess({"TEST_DATABASE_URL": None})
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "TEST_DATABASE_URL" in combined
    # collectionまで進んでいれば "1 passed" が出るはずである。exitはそれより
    # 前に起きるので、trivialなテストすら実行されていないことを確認する。
    assert "1 passed" not in combined


def test_production_like_database_name_fails_before_collection() -> None:
    """DB名が `autoscreener_test` で終わらない(本番相当 `autoscreener` 等)なら
    非0終了する——変数は設定したが値を間違えた事故を防ぐガード。"""
    result = _run_pytest_subprocess(
        {"TEST_DATABASE_URL": "postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener"}
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "autoscreener_test" in combined
    assert "1 passed" not in combined


def test_valid_test_database_url_allows_collection_to_proceed() -> None:
    """対照実験:正しい `TEST_DATABASE_URL` なら、trivialなテストは実行される。

    実際のDBへの到達性は問わない(`support/dummy_ok.py` はDBに触れない)——
    ここで確認したいのは「ガードが正しい入力まで弾いていないか」だけである。
    """
    result = _run_pytest_subprocess(
        {"TEST_DATABASE_URL": "postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test"}
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in result.stdout
