"""tests/unit/test_rate_limit.py(2026-08-30)。ネットワークにもDBにも触れない。

固定しているのは、**レート制御が黙って効かなくなる**3つの経路である。
どれも例外を出さずに「速く投げてしまう」だけなので、テストが無いと気づけない
——気づくのはSECに遮断されたときになる。

1. `EdgarClient` を複数作ってもリミッターが1つであること
2. リトライがリミッターを素通りしないこと
3. サーバの「待て」(429 の `Retry-After`・403 の遮断)が全体を止めること
"""

from __future__ import annotations

import threading
import time

import pytest
import requests
import responses

from autoscreener.collectors.edgar_client import COMPANY_TICKERS_URL, EdgarClient
from autoscreener.collectors.errors import PermanentFailure, TransientFailure
from autoscreener.collectors.rate_limit import (
    RateLimiter,
    configure_shared_limiter,
    get_shared_limiter,
    reset_shared_limiters,
)
from autoscreener.config import EdgarConfig, EdgarRetryConfig

_USER_AGENT = "TENX test <test@example.com>"


def _config(**overrides) -> EdgarConfig:
    base = dict(
        enabled=True,
        requests_per_second=10.0,  # 上限いっぱい(le=10)。テストを遅くしない値
        timeout_seconds=5.0,
        document_fetch_enabled=True,
        max_tracked_tickers=10,
        throttle_cooldown_seconds=0.2,  # 本番は60秒。テストでは待てないので短く
        retry=EdgarRetryConfig(max_attempts=3, backoff_base_seconds=0.01, backoff_max_seconds=0.02),
    )
    base.update(overrides)
    return EdgarConfig(**base)


# ---------------------------------------------------------------------------
# 1. 共有されていること
# ---------------------------------------------------------------------------


def test_every_edgar_client_shares_one_limiter():
    """**これが今回の本丸。** クライアントごとに持つと実効レートが倍々になる。

    コード上 `EdgarClient(...)` を作る箇所は9つあり、SECはIP単位で数えるので、
    インスタンスごとのリミッターでは設定値の最大9倍のレートで投げてしまう。
    """
    a = EdgarClient(_config(), _USER_AGENT)
    b = EdgarClient(_config(), _USER_AGENT)
    assert a._rate_limiter is b._rate_limiter
    assert a._rate_limiter is get_shared_limiter("sec")


def test_new_client_does_not_reset_the_interval():
    """新しいクライアントを作っても「直前に投げた」事実は消えないこと。

    以前は `_last_call = None` から始まるため、直前に別のクライアントが投げた
    ばかりでも1本目を即座に送っていた。`run-daily-pipeline` はEDGARを使う工程を
    連続して回すので、工程の境目で毎回これが起きていた。
    """
    # `EdgarConfig.requests_per_second` の上限は10(le=10)なので、間隔は100ms。
    limiter = configure_shared_limiter("sec", 10.0)
    limiter.acquire()
    EdgarClient(_config(requests_per_second=10.0), _USER_AGENT)  # 作り直す
    start = time.monotonic()
    get_shared_limiter("sec").acquire()
    assert time.monotonic() - start >= 0.09


def test_config_is_authoritative_over_the_safe_default():
    """設定ファイルの値がリミッターに反映されること(既定値に固定されない)。"""
    assert get_shared_limiter("sec").requests_per_second == pytest.approx(5.0)
    configure_shared_limiter("sec", 3.0)
    assert get_shared_limiter("sec").requests_per_second == pytest.approx(3.0)


def test_unconfigured_key_falls_back_to_a_safe_default_not_unlimited():
    """設定を読む経路を通らなかった呼び出しでも無制限にならないこと。

    APIプロセスからの単発のFX取得(`GET /fx/usdjpy`)がこの経路に当たる。
    """
    assert get_shared_limiter("yfinance").requests_per_second == pytest.approx(2.0)


def test_unknown_key_is_an_error_not_a_silent_pass_through():
    """知らない用途を無制限で通さない(打ち間違いを黙って許さない)。"""
    with pytest.raises(KeyError):
        get_shared_limiter("nope")


# ---------------------------------------------------------------------------
# 2. 間隔と休止
# ---------------------------------------------------------------------------


def test_minimum_interval_is_enforced():
    limiter = RateLimiter(20.0)  # 50ms
    start = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    # 1本目は即時なので、待ちは3間隔ぶん。
    assert time.monotonic() - start >= 0.15


def test_pause_for_stops_everything_until_it_expires():
    limiter = RateLimiter(1000.0)
    limiter.pause_for(0.2)
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start >= 0.19


def test_a_shorter_pause_does_not_shorten_a_longer_one():
    """短い休止で上書きして早く再開してしまわないこと。"""
    limiter = RateLimiter(1000.0)
    limiter.pause_for(0.25)
    limiter.pause_for(0.01)
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start >= 0.2


def test_pause_is_not_blocked_by_a_sleeping_thread():
    """待機中のスレッドがいても `pause_for` が即座に効くこと。

    旧実装はロックを保持したまま `sleep` していたので、「今すぐ全員止める」が
    最大1間隔ぶん遅れた。ここではロックの外で待つようにしてある。
    """
    limiter = RateLimiter(2.0)  # 500ms間隔
    limiter.acquire()
    threading.Thread(target=limiter.acquire, daemon=True).start()
    time.sleep(0.05)  # 別スレッドを待機状態に入らせる
    start = time.monotonic()
    limiter.pause_for(0.1)
    assert time.monotonic() - start < 0.1, "pause_for がロック待ちでブロックされた"


def test_concurrent_acquires_still_respect_the_interval():
    """並列でも合計レートが上限を超えないこと。"""
    limiter = RateLimiter(20.0)  # 50ms
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 5
    # 5本を50ms間隔で流すので、最後の1本までに4間隔=200ms かかる。
    assert max(stamps) - start >= 0.19


# ---------------------------------------------------------------------------
# 3. サーバの「待て」に従うこと
# ---------------------------------------------------------------------------


@responses.activate
def test_retries_go_through_the_limiter_too():
    """**リトライがレート制御を素通りしないこと。**

    以前は `_get` の入口で1回だけ acquire していたため、`max_attempts` 回の
    再送は間隔制御を通っていなかった——しかもリトライが起きるのは相手が
    詰まっているときなので、最も投げてはいけない場面で最も速く投げていた。
    """
    responses.add(responses.GET, COMPANY_TICKERS_URL, status=503)
    client = EdgarClient(_config(requests_per_second=10.0, throttle_cooldown_seconds=0.05), _USER_AGENT)

    start = time.monotonic()
    with pytest.raises(TransientFailure):
        client.fetch_company_tickers()
    elapsed = time.monotonic() - start

    assert len(responses.calls) == 3  # max_attempts
    # 3本ぶんの間隔(2×100ms)は最低でもかかる。0回acquireなら一瞬で終わっていた。
    assert elapsed >= 0.2


@responses.activate
def test_retry_after_header_is_honoured():
    """429 の `Retry-After` に従うこと。

    tenacity の指数バックオフ(初期0.01秒)だけに任せると、サーバの指定より
    はるかに早く再送してしまう。
    """
    responses.add(responses.GET, COMPANY_TICKERS_URL, status=429, headers={"Retry-After": "0.3"})
    client = EdgarClient(
        _config(requests_per_second=10.0, throttle_cooldown_seconds=0.01), _USER_AGENT
    )

    start = time.monotonic()
    with pytest.raises(TransientFailure):
        client.fetch_company_tickers()
    # 2回目・3回目の再送前にそれぞれ0.3秒待つので、合計0.6秒は下回らない。
    assert time.monotonic() - start >= 0.55


@responses.activate
def test_a_403_pauses_every_subsequent_sec_request():
    """403(遮断)を受けたら、**残りの銘柄が叩き続けないように全体を止める**。

    403は `PermanentFailure` なので個別のリクエストは再試行されないが、
    止めなければ次の銘柄が即座に次の403を取りに行く。300銘柄あれば遮断中の
    SECに300本投げることになり、遮断が長引く。
    """
    responses.add(responses.GET, COMPANY_TICKERS_URL, status=403)
    client = EdgarClient(_config(throttle_cooldown_seconds=0.3), _USER_AGENT)

    with pytest.raises(PermanentFailure):
        client.fetch_company_tickers()

    start = time.monotonic()
    get_shared_limiter("sec").acquire()  # 次の銘柄にあたる
    assert time.monotonic() - start >= 0.25


@responses.activate
def test_a_successful_response_does_not_pause_anything():
    """正常系で余計な休止が入らないこと(冷却の入れどころを間違えていない)。"""
    responses.add(responses.GET, COMPANY_TICKERS_URL, json={"0": {"ticker": "AAPL", "cik_str": 320193}})
    client = EdgarClient(_config(), _USER_AGENT)
    assert client.fetch_company_tickers() == {"AAPL": "0000320193"}

    start = time.monotonic()
    get_shared_limiter("sec").acquire()
    assert time.monotonic() - start < 0.2


@responses.activate
def test_unparseable_retry_after_falls_back_to_the_configured_cooldown():
    """`Retry-After` がHTTP-date形式などで読めないとき、解釈を誤らず既定に倒すこと。"""
    responses.add(
        responses.GET,
        COMPANY_TICKERS_URL,
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    client = EdgarClient(
        _config(requests_per_second=10.0, throttle_cooldown_seconds=0.15), _USER_AGENT
    )
    start = time.monotonic()
    with pytest.raises(TransientFailure):
        client.fetch_company_tickers()
    # 読めなくても冷却は入る(0.15秒 × 再送2回)。
    assert time.monotonic() - start >= 0.25


# ---------------------------------------------------------------------------
# yfinance 側
# ---------------------------------------------------------------------------


def test_yfinance_calls_go_through_a_shared_ceiling():
    """yfinanceの各呼び出しが秒あたり上限を通ること。

    ジッタ(`parallel_runner`)は1銘柄あたり1回で、しかも送信間隔を揺らすだけ
    ——**Yahooが速く返すほど実効レートが上がる**という性質があった。天井は
    リミッター側で持つ。
    """
    from autoscreener.collectors import yfinance_client

    configure_shared_limiter("yfinance", 20.0)  # 50ms
    start = time.monotonic()
    for _ in range(4):
        yfinance_client._throttle()
    # 1本目は即時なので、待ちは3間隔ぶん。
    assert time.monotonic() - start >= 0.15


def test_fx_rate_fetch_is_throttled(monkeypatch):
    """APIプロセスからの単発FX取得も天井を通ること(設定を読まない経路)。"""
    from autoscreener.collectors import yfinance_client

    yfinance_client._fx_rate_cache.clear()
    acquired: list[str] = []

    class _Limiter:
        def acquire(self) -> None:
            acquired.append("x")

    monkeypatch.setattr(yfinance_client, "get_shared_limiter", lambda key: _Limiter())

    class _Boom:
        def __init__(self, *_a, **_k) -> None:
            raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(yfinance_client.yf, "Ticker", _Boom)

    assert yfinance_client.fetch_fx_rate("EUR", "USD") is None
    assert acquired, "FX取得がリミッターを通っていない"


def test_reset_clears_shared_state():
    configure_shared_limiter("sec", 3.0)
    reset_shared_limiters()
    assert get_shared_limiter("sec").requests_per_second == pytest.approx(5.0)
