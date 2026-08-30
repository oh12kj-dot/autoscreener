"""外部APIへのリクエストレート制御(2026-08-30)。

**なぜ独立したモジュールにしたか。** これまで `RateLimiter` は
`edgar_client.py` の中にあり、`EdgarClient` のインスタンスごとに1つ作られて
いた。ところがコード上には `EdgarClient(...)` を作る箇所が9つある
(`collect_filings` / `collect_xbrl_facts` / `collect_filing_sections` /
`collect_concentration` / `collect_dilution` / `collect_supply` /
`refresh_cik_map` / `delisting_source` の2箇所)。

**SECはIP単位で数える。プロセス内に9個のリミッターがあっても、向こうから見れば
1つの送信元である。** 実効レートは設定値の最大9倍になりうるうえ、新しい
クライアントは `_last_call = None` から始まるので、直前に別のクライアントが
投げたばかりでも即座に1本目を送る。`run-daily-pipeline` は EDGAR を使う工程を
連続して回すので、この取りこぼしは毎回起きていた。

したがってリミッターは**用途(SEC / yfinance)ごとにプロセスで1つ**にする。

**プロセスを跨いだ協調はしない。** ファイルロックで複数プロセスを同期させる
ことは技術的には可能だが、ロックの取り残し(異常終了時)という新しい故障モードを
持ち込む割に、この用途——個人利用のバッチ——では「同時に何本も走らせない」
という運用で足りる。**同じマシンで日次パイプラインとテストスイートを同時に
走らせれば、レートは単純に足し算になる**という事実は、設定値の余裕
(`edgar.requests_per_second` を上限10に対して5にしてある)で吸収する。

**トークンバケットではなく「最小間隔」方式**なのは以前のままである。バケット
方式は蓄積したトークンでバーストが起きうるが、SECの制限は瞬間レートに対する
ものなので、間隔を一定に保つほうが安全側に倒れる。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 設定が渡されなかった場合の安全側の既定。**呼び出し側が設定を読み忘れても
# 無制限にはならない**ようにするための下限であって、推奨値ではない
# (推奨値は `config/collection.yaml` にある)。
_DEFAULT_RATES: dict[str, float] = {
    # SECの公表上限は約10 req/s。ここを既定の半分にしてあるのは、同じIPから
    # 出ていく他のリクエスト(ブラウザ・別プロセスのバッチ)ぶんの余裕を
    # 最初から見込んでおくため。
    "sec": 5.0,
    # Yahooは上限を公表していない。実測(2026-08-23)では並列8で30%が
    # transient failure になった。秒あたりで抑える側の歯止めとして2.0を置く。
    "yfinance": 2.0,
}


class RateLimiter:
    """「最小間隔」方式のレート制御。スレッドセーフ。

    `time.monotonic()` を使うのはシステム時刻の変更に影響されないため。

    `pause_for()` は、サーバから「待て」と言われたとき(429 の `Retry-After`、
    403 の遮断)に**次の1本だけでなく、その用途の全リクエストを一斉に止める**
    ためにある。1本ずつ個別にバックオフすると、遮断中のサーバに対して
    銘柄数ぶんのリクエストを投げ続けることになり、遮断が長引く。
    """

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self._lock = threading.Lock()
        self._min_interval = 1.0 / requests_per_second
        self._last_call: float | None = None
        # この時刻まで誰も送信しない(monotonic基準)。
        self._paused_until: float = 0.0

    @property
    def requests_per_second(self) -> float:
        return 1.0 / self._min_interval

    def set_rate(self, requests_per_second: float) -> None:
        """レートを設定し直す。設定ファイルの値を反映するために使う。"""
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        with self._lock:
            self._min_interval = 1.0 / requests_per_second

    def pause_for(self, seconds: float) -> None:
        """今から `seconds` 秒、この用途の全リクエストを止める。

        既に入っている休止のほうが長ければ、そちらを尊重する(短い休止で
        上書きして早く再開してしまわないため)。
        """
        if seconds <= 0:
            return
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def acquire(self) -> None:
        """送信してよくなるまで待つ。

        **ロックを保持したまま眠らない。** 旧実装(`edgar_client.RateLimiter`)は
        ロック内で `sleep` していた。間隔の制御としてはそれで正しく動くが、
        `pause_for()` を足すと問題になる——遮断を検知したスレッドが休止を
        設定しようとしても、別のスレッドが眠っている間はロックを取れず、
        **「今すぐ全員止める」が最大で1間隔ぶん遅れる**。ここでは「次に送って
        よい時刻」をロック内で確定させてから、待つのはロックの外で行う。

        待ち時間を計算し直すためにループしている。複数スレッドが同じ時刻に
        起きても、枠を取れるのは1本だけで、残りは次の間隔まで待ち直す。
        """
        while True:
            with self._lock:
                now = time.monotonic()
                earliest = self._paused_until
                if self._last_call is not None:
                    earliest = max(earliest, self._last_call + self._min_interval)
                if now >= earliest:
                    # 送信枠を確保する。以降のスレッドはこの時刻を基準に待つ。
                    self._last_call = now
                    return
                wait = earliest - now
            time.sleep(wait)


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def get_shared_limiter(key: str) -> RateLimiter:
    """`key`(用途)ごとにプロセスで1つのリミッターを返す。

    未設定なら `_DEFAULT_RATES` の安全側の値で作る——設定を読み込む経路を
    通らなかった呼び出し(APIプロセスからの単発のFX取得など)でも、
    無制限にはならないようにするため。
    """
    with _limiters_lock:
        limiter = _limiters.get(key)
        if limiter is None:
            rate = _DEFAULT_RATES.get(key)
            if rate is None:
                raise KeyError(f"unknown rate limiter key {key!r}; add it to _DEFAULT_RATES")
            limiter = RateLimiter(rate)
            _limiters[key] = limiter
        return limiter


def configure_shared_limiter(key: str, requests_per_second: float) -> RateLimiter:
    """設定ファイルの値をリミッターに反映する。

    **設定を正とする**(既定値より速い値でも受け入れる)。緩める方向にも動く
    ので、呼ぶのは設定を読んだ場所だけにすること。`collection.yaml` は
    1つしか無いため、複数の呼び出し元があっても値は一致する。
    """
    limiter = get_shared_limiter(key)
    if abs(limiter.requests_per_second - requests_per_second) > 1e-9:
        logger.debug(
            "rate limiter %s: %.2f -> %.2f req/s", key, limiter.requests_per_second, requests_per_second
        )
        limiter.set_rate(requests_per_second)
    return limiter


def reset_shared_limiters() -> None:
    """テスト用。プロセス内の共有状態を捨てる。

    共有リミッターは意図的にプロセス全体で1つなので、あるテストが設定した
    レートや休止が後続のテストに漏れる。それを断ち切るためのフック。
    """
    with _limiters_lock:
        _limiters.clear()
