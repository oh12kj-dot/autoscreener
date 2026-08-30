"""全テスト共通の前処理。

ここにあるのは2つとも「**失敗しても例外が出ず、ただ遅くなるだけ**」という
種類の事故に対する歯止めである。遅いだけの失敗はテストが緑のまま通るので、
仕組みで塞がないと気づけない。
"""

from __future__ import annotations

import socket

import pytest

from autoscreener.collectors.rate_limit import reset_shared_limiters


@pytest.fixture(autouse=True)
def _isolate_rate_limiters():
    """各テストの前後で共有レートリミッターを初期化する。

    `collectors/rate_limit.py` のリミッターは意図的にプロセス全体で1つであり、
    レートと休止状態を保持する。SECがIP単位で数える以上そうあるべきなのだが、
    テストでは前のテストの状態が次のテストに漏れる。

    実際に起きた事故(2026-08-30):`test_http_403_is_permanent_failure_not_retried`
    が403を受けた時点で `throttle_cooldown_seconds`(60秒)の全体休止が入り、
    **次のテストが60秒ブロックされた**(ファイル全体が1秒→61秒)。本番では
    それが正しい挙動——遮断中のSECを叩き続けないため——なので、直すべきは
    リミッター側ではなくテストの独立性のほうである。
    """
    reset_shared_limiters()
    yield
    reset_shared_limiters()


# ローカル開発用Postgres(docker compose)だけは通す。テストはDBには触れる。
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


class OutboundNetworkBlocked(RuntimeError):
    """テスト中に外部への接続が試みられた。"""


def _host_of(address) -> str | None:
    """`connect()` に渡されたアドレスからホスト部分を取り出す。

    AF_INET は `(host, port)`、AF_INET6 は `(host, port, flowinfo, scopeid)`。
    UNIXソケット等は文字列なので、その場合は判定しない(通す)。
    """
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None


@pytest.fixture(autouse=True)
def _block_outbound_network(request):
    """**テストから外部ネットワークへ出さない。**

    実際に起きた事故(2026-08-30):`test_daily_pipeline.py` の自動適用
    フィクスチャが J-6/J-7 で足された工程(`collect_events` /
    `collect_insider` / `collect_short_interest`)をパッチし忘れていたため、
    6つのテストがそれぞれ**最大300銘柄ぶんのSEC・FINRA・Yahooへの実アクセス**を
    発生させていた。テストは落ちない(工程の失敗は握りつぶす設計なので)。
    ただ終わらないだけ——**テストスイートが数時間かかり、SECにIP単位の
    レート制限をかけられる**という形で表に出た。

    パッチ漏れは今後も起きる(工程を足す人がこのフィクスチャを知らない)ので、
    「漏れたら遅くなる」ではなく「漏れたら落ちる」に変える。落ちれば、どの
    テストがどのホストへ出ようとしたかがその場で分かる。

    実ネットワークが要るテストには `@pytest.mark.live_network` を付けること
    (現状そのようなテストは無い)。
    """
    if request.node.get_closest_marker("live_network"):
        yield
        return

    def _guard(self, address):
        host = _host_of(address)
        if host is not None and host not in _ALLOWED_HOSTS:
            raise OutboundNetworkBlocked(
                f"テストから外部への接続が試みられました: {host}。"
                "テストは実APIを叩いてはいけません(SEC/Yahoo/FINRAのレート制限を"
                "消費し、スイートが終わらなくなります)。呼び出し先をモックするか、"
                "fetcher を注入してください。"
                "本当に実アクセスが必要なら @pytest.mark.live_network を付けます。"
            )
        return _real_connect(self, address)

    def _guard_ex(self, address):
        host = _host_of(address)
        if host is not None and host not in _ALLOWED_HOSTS:
            raise OutboundNetworkBlocked(f"テストから外部への接続が試みられました: {host}")
        return _real_connect_ex(self, address)

    socket.socket.connect = _guard
    socket.socket.connect_ex = _guard_ex
    try:
        yield
    finally:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_network: 実際の外部APIへ接続するテスト(既定では接続がブロックされる)",
    )
