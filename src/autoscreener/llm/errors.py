"""LLM呼び出しの失敗分類(K-9)。`collectors/errors.py` と同じ思想。

ネットワーク越しの呼び出しを `except Exception` でひとまとめにすると、
「このリクエストは何度投げても400で落ちる」と「1分待てば通る」が同じ扱いに
なる——必要なリトライ挙動は正反対である。加えてLLM固有の失敗が2つある:

- **拒否(`LlmRefusal`)**:HTTP 200 で返ってくる。`stop_reason == "refusal"`
  を見ないと、空文字列を「要約が空だった」として保存してしまう。
- **打ち切り(`LlmTruncated`)**:`stop_reason == "max_tokens"`。これも HTTP 200
  で、しかも**それらしい文章が途中まで入っている**のが厄介である。黙って
  保存すると「リスク要因は3つ」と書かれた4つ目以降が消えた要約が、完成品と
  区別できない形で残る。`Filing.analysis` が「解析したが無かった」と
  「解析していない」を区別しているのと同じ理由で、ここは必ず落とす。

`LlmInputTooLarge` も同じ系列にある。入力を黙って切り詰めるくらいなら失敗
させる——切られた10-Kの要約は、読んでいない箇所について何も言わないが、
出力からはそれが分からない。
"""

from __future__ import annotations

import anthropic


class LlmError(Exception):
    """LLM層の分類済み失敗すべての基底クラス。"""


class LlmDisabled(LlmError):
    """`llm.enabled = false` か `ANTHROPIC_API_KEY` 未設定。

    失敗ではなく「使わない構成」である。呼び出し側はこれを握って
    「LLM機能なし」として正常終了してよい(FRED未設定時と同じ扱い)。
    """


class LlmTransientFailure(LlmError):
    """リトライで回復しうる失敗(429・5xx・接続断・タイムアウト)。

    SDK自身が既定で2回リトライした**あと**にこれが上がる。つまりここまで
    来た時点で既に数回試しているので、呼び出し側は銘柄単位で諦めて次へ進み、
    実行全体は止めない。
    """


class LlmPermanentFailure(LlmError):
    """投げ直しても直らない失敗(400・401・403・404)。

    APIキーが無効、モデルIDが存在しない、リクエストが仕様違反——いずれも
    コードか設定を直すまで100%再現する。リトライはレート制限を食うだけなので
    しない。
    """


class LlmRefusal(LlmError):
    """安全性の理由でモデルが応答を拒否した(`stop_reason == "refusal"`)。

    `category` は開いた集合(`"cyber"` / `"bio"` 等)で、`None` もありうる。
    """

    def __init__(self, message: str, category: str | None = None) -> None:
        self.category = category
        super().__init__(message)


class LlmTruncated(LlmError):
    """`max_tokens` に当たって出力が途中で切れた。

    `max_output_tokens` を上げるか、入力を分割する。**部分的な出力は保存しない**。
    """


class LlmInputTooLarge(LlmError):
    """入力が `llm.max_input_chars` を超えた。

    切り詰めずに落とす(モジュールdocstring参照)。`chars` に実際の文字数を
    持たせ、運用者が「どれだけ超えたか」を見て設定を上げるか分割するかを
    判断できるようにする。
    """

    def __init__(self, message: str, chars: int, limit: int) -> None:
        self.chars = chars
        self.limit = limit
        super().__init__(message)


class LlmParseFailure(LlmError):
    """構造化出力がスキーマに合わなかった、または期待した内容ブロックが無い。

    `collectors/errors.py` の `ParseFailure` と同じく**最優先で表に出す失敗**。
    構造化出力(`output_config.format`)を使っている以上これは起きないはずで、
    起きたならSDKかAPIの契約が変わったということ——黙ってリトライしてはならない。
    """


def _openai_classified(exc: Exception) -> LlmError | None:
    """OpenAI互換SDK(`llm.provider = openai_compat`)の例外を写す。

    `openai` は必須依存だが、import 失敗でも Anthropic 経路が死なないよう
    ガードしておく。`openai.APIError` 系の階層は `anthropic` とほぼ同型なので
    対応は素直——認証・不正リクエスト・404 は恒久、429・5xx・接続断は一時的。
    """
    try:
        import openai
    except ImportError:  # pragma: no cover - openai は pyproject の必須依存
        return None

    if isinstance(
        exc,
        (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,
            openai.BadRequestError,
            openai.UnprocessableEntityError,
        ),
    ):
        return LlmPermanentFailure(f"{type(exc).__name__}: {exc}")
    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.ConflictError,
        ),
    ):
        return LlmTransientFailure(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and status >= 500:
            return LlmTransientFailure(f"HTTP {status}: {exc}")
        return LlmPermanentFailure(f"HTTP {status}: {exc}")
    return None


def classify_exception(exc: Exception) -> LlmError:
    """LLM SDK(Anthropic / OpenAI互換)の例外を上の分類へ写す。

    未知の例外型は `LlmParseFailure` にする(`collectors.errors.
    classify_exception` と同じ理由——分類できない失敗を「一時的」として
    黙ってリトライするのが一番まずい)。
    """
    if isinstance(exc, LlmError):
        return exc

    openai_result = _openai_classified(exc)
    if openai_result is not None:
        return openai_result

    # 認証・権限・不正リクエスト・存在しないモデルは投げ直しても直らない。
    if isinstance(
        exc,
        (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
            anthropic.UnprocessableEntityError,
            anthropic.RequestTooLargeError,
        ),
    ):
        return LlmPermanentFailure(f"{type(exc).__name__}: {exc}")

    if isinstance(
        exc,
        (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.ServiceUnavailableError,
            anthropic.OverloadedError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.ConflictError,
        ),
    ):
        return LlmTransientFailure(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and status >= 500:
            return LlmTransientFailure(f"HTTP {status}: {exc}")
        return LlmPermanentFailure(f"HTTP {status}: {exc}")

    return LlmParseFailure(f"unclassified exception {type(exc).__name__}: {exc}")
