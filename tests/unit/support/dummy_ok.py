"""A-1(docs/racr_wp_a_operational_safety_2026-09-04.md)の回帰テストが使う
使い捨てターゲット。

ファイル名をあえて `test_*.py` にしていない——通常の `pytest tests/` 実行
(`pyproject.toml` の `testpaths = ["tests"]`)ではディレクトリ走査時の
`python_files` パターンに一致せず収集されない。`test_conftest_db_isolation.py`
がサブプロセスでこのファイルのパスを明示的に指定したときだけ収集される。

中身に意味は無い(DBにもネットワークにも触れない)。存在理由は「A-1のDB隔離
ガードが正しく機能していれば、ここへ辿り着く前に `pytest.exit()` される」
ことを確認するための、安全な(=万一ガードが壊れていても実害の無い)標的
であることだけである。
"""


def test_dummy_ok() -> None:
    assert True
