"""全テスト共通の前処理。

ここにある歯止めは3種類の事故に対応する。前の2つは「**失敗しても例外が出ず、
ただ遅くなるだけ**」という種類の事故で、遅いだけの失敗はテストが緑のまま通る
ので、仕組みで塞がないと気づけない。3つ目(A-1)は逆に「**失敗しても例外が
出ず、本番相当のデータを黙って壊す**」という、遅くなるより悪い種類の事故で
あり、テストが1つも走る前に必ず落とす(fail closed)。
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

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



_PRODUCTION_LIKE_SUFFIX_REQUIREMENT = "autoscreener_test"


def _fail_closed(message: str) -> None:
    """collection開始前にpytest自体を終了させる(A-1)。

    `pytest.skip()` ではなく `pytest.exit()` を使う。skipは「緑の中に紛れて
    見逃される」——CIやローカルの実行結果が"xxx passed, 1 skipped"に見えても、
    それが「DB隔離ガードがブロックした」ことだとは誰も気づかない。DBを壊す
    事故は、通常のテスト失敗よりも強い形(プロセス自体の即時終了)で
    知らせなければならない。
    """
    pytest.exit(message, returncode=1)


def _require_isolated_test_database() -> str:
    """A-1(docs/racr_wp_a_operational_safety_2026-09-04.md、監査§10.2/10.4):

    テストfixtureの多くはTickerを作成・削除する
    (例:test_apply_gates_point_in_time.py の `_cleanup()`)。これが通常の
    開発用DB(`autoscreener`。`.env`の既定 `DATABASE_URL` が指す先)へ
    書き込むと、日次パイプラインの実行と衝突しうる——2026-09-03の
    gate stage FK違反(`ticker_id=24528` が消えていた)は、テストDBが
    分離されていないことが有力仮説として挙がっている。

    ここで2つを強制する:

    1. `TEST_DATABASE_URL` が設定されていること(未設定は「たまたま
       ローカルDBに繋がって動いてしまう」を許すため、既定値へは絶対に
       フォールバックしない)。
    2. そのDB名が `autoscreener_test` で終わること(本番相当の
       `autoscreener` を指定したら、たとえ環境変数名は正しくても弾く——
       「変数は設定したが値を間違えた」事故を防ぐ)。
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        _fail_closed(
            "TEST_DATABASE_URL が設定されていません。テストは専用のPostgreSQL"
            "データベース(例: autoscreener_test)にのみ接続できます。通常の"
            "開発用DB(autoscreener)へテストが接続すると、テストfixtureが"
            "Tickerを作成・削除し、日次パイプラインの実行と衝突します"
            "(2026-09-03のgate stage FK違反の有力仮説。"
            "docs/racr_wp_a_operational_safety_2026-09-04.md 参照)。\n"
            "  1) docker compose の Postgres に対して: "
            "`docker exec <db container> psql -U autoscreener -d autoscreener "
            "-c \"CREATE DATABASE autoscreener_test OWNER autoscreener;\"`\n"
            "  2) `DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@"
            "localhost:5432/autoscreener_test uv run alembic upgrade head`\n"
            "  3) `TEST_DATABASE_URL=postgresql+psycopg://autoscreener:autoscreener@"
            "localhost:5432/autoscreener_test uv run pytest`"
        )

    db_name = urlsplit(url).path.lstrip("/")
    if not db_name.endswith(_PRODUCTION_LIKE_SUFFIX_REQUIREMENT):
        _fail_closed(
            f"TEST_DATABASE_URL のDB名 {db_name!r} は "
            f"{_PRODUCTION_LIKE_SUFFIX_REQUIREMENT!r} で終わっていません。"
            "本番相当のDB(autoscreener等)をテストが書き換えるのを防ぐための"
            "強制チェックです。専用のテストDBを指すURLを設定してください。"
        )
    return url


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_network: 実際の外部APIへ接続するテスト(既定では接続がブロックされる)",
    )

    test_database_url = _require_isolated_test_database()

    # A-1:`autoscreener.config.get_settings()` はキャッシュを持たず、呼ぶたび
    # に `Settings()` を再構築する(pydantic-settingsが環境変数を都度読む)。
    # よって「テストDBへ差し替える」ために個々のモジュールが束縛済みの
    # `get_settings` 参照を書き換える必要は無く、**プロセス環境変数を上書き
    # するだけで全呼び出し元に効く**——`.env` の既定値より環境変数のほうが
    # 優先されるため、`.env` の DATABASE_URL(本番相当)は無視される。
    os.environ["DATABASE_URL"] = test_database_url

    # `api.dependencies.get_session()`(APIレイヤーの読み取り専用接続、18.6)は
    # `api_database_url`(未設定なら `database_url` にフォールバック)を使う。
    # `.env` の既定 `API_DATABASE_URL` は本番相当DBの読み取り専用ロールを
    # 指しているため、ここを上書きしないと **FastAPI経由のテスト
    # (`TestClient` でHTTPリクエストを送るテスト)だけが本番相当DBへ
    # 接続してしまう**——書き込みは読み取り専用ロールなので防げるが、
    # テストの結果が実運用データの状態に左右される(非決定的になる)のは
    # 変わらない。`TEST_DATABASE_URL` へ同じ値で上書きし、書き込み経路
    # (`database_url`)と読み取り経路(`api_database_url`)を必ず同じテストDBへ
    # 揃える。
    os.environ["API_DATABASE_URL"] = test_database_url

    # `db/session.py`(バッチ・書き込み層)と `api/dependencies.py`(API・
    # 読み取り層)、両方のグローバルキャッシュ(`_engine`/`_SessionFactory`)を
    # リセットする。通常はpytest起動時点でまだ誰も `get_engine()` /
    # `get_session()` を呼んでいないため無害だが、将来別のpytestプラグイン/
    # conftestがテスト収集より前にDBへ触れるようになった場合に備え、
    # 「本番DBへ接続したengineがキャッシュされたまま残る」事故を防ぐ防御を
    # 明示しておく。
    from autoscreener.api import dependencies as api_dependencies
    from autoscreener.db import session as db_session

    db_session._engine = None
    db_session._SessionFactory = None
    api_dependencies._api_engine = None
    api_dependencies._ApiSessionFactory = None
