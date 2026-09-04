"""tests/unit/test_yfinance_throttle_import_isolation.py(S-1/S-4監査、
docs/daily_pipeline_throughput_plan_2026-09-04.md)。

`_install_http_throttle()`(S-1)は`collectors/yfinance_client`が
**importされた時点で**`yfinance.data.YfData._make_request`をモンキーパッチ
する副作用であり、プロセス起動時に自動でかかる保証ではない。この保証が
本当に構造的(=どのモジュールをどんな順序でimportしても成り立つ)かどうかは、
「そのモジュール**だけ**をフレッシュなインタプリタへimportした時点で
スロットルが入っているか」でしか確認できない——同じpytestプロセス内では
他のテストファイルが既に`yfinance_client`をimport済みなので、何をimport
しても「入っている」ように見えてしまい偽陽性になる。

2026-09-04の監査で見つかった実例(同じ欠陥クラスの2つのインスタンス):

- `collectors/consensus.py`(`batch/collect_consensus.py`が依存、S-4で
  並列化済み):`YfinanceConsensusProvider.__init__`が独立に
  `import yfinance as yf`していたため、`collect_consensus()`を直接呼ぶ
  経路(スクリプト・将来のCLI配線・単体テスト)ではスロットルが一切
  入らないままmax_workers並列でYahooに投げる状態が起こりえた。
- `collectors/calendar_source.py`(`batch/collect_events.py`が依存する
  週次`events`工程):`fetch_calendar()`が同じパターンで独立に
  `import yfinance as yf`していた。

どちらも`run-daily-pipeline`では偶然安全だった——`cli.py`が
`snapshot_collector`経由で`yfinance_client`を別途importしていたため。
だがこれはそれぞれのモジュール自身の設計とは無関係な偶然でしかなく、
別の呼び出し経路からは無防備だった。修正はどちらも同じ形:
`collectors/yfinance_client`をモジュール直下でimportし、その`yf`参照
(`_yfinance_client.yf.Ticker`)を使うことで、「このモジュールが
importされればスロットルが入る」ことをimport順序に依存しない構造的な
保証にした。

**新しいyfinance連携を足すとき**:このモジュールが(直接・間接問わず)
`collectors/yfinance_client`をimportしているか確認し、エントリポイントを
`_ENTRY_POINT_MODULES`に追加すること。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# yfinanceへ到達しうる、実際のエントリポイント(バッチ工程の入口と、
# それが依存するcollectors側モジュール)。
_ENTRY_POINT_MODULES = [
    "autoscreener.batch.collect_consensus",
    "autoscreener.collectors.consensus",
    "autoscreener.batch.collect_events",
    "autoscreener.collectors.calendar_source",
]


@pytest.mark.parametrize("module_name", _ENTRY_POINT_MODULES)
def test_importing_module_in_isolation_installs_the_http_throttle(module_name: str):
    """`module_name`だけをフレッシュなインタプリタでimportしても、
    `YfData._make_request`(実HTTP境界、S-1)へのスロットルが入っていること。

    フレッシュな**サブプロセス**で検証する(同じpytestプロセス内で
    importしても、他のテストファイルが既に`yfinance_client`を
    import済みなので偽陽性になる)。
    """
    script = (
        "import yfinance.data as yf_data\n"
        f"import {module_name}\n"
        "installed = getattr(\n"
        "    yf_data.YfData._make_request, '_autoscreener_http_throttle_installed', False\n"
        ")\n"
        "print('THROTTLE_INSTALLED' if installed else 'THROTTLE_NOT_INSTALLED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"サブプロセスがエラー終了した({module_name}): {result.stderr}"
    assert "THROTTLE_INSTALLED" in result.stdout, (
        f"{module_name} をimportしただけではスロットルが入っていない "
        f"(stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
