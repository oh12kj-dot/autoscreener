"""LLM SDK の薄いラッパ(K-9)。Anthropic native と OpenAI 互換の2実装。

**このモジュールだけがネットワークに触れる。** `filing_summary` /
`qualitative` / `report` はプロンプトを組み立てて応答を解釈するだけの
純関数に保つ——`collectors/filing_text.py` が EDGAR に触れず切り出しだけを
するのと同じ理由で、テストしやすさと再利用性のため。

`build_provider()` が `llm.provider` を見て `LlmClient`(Anthropic)か
`OpenAICompatClient`(ChatGPT / NVIDIA NIM / Ollama 等)を返す。両者は
`complete_text` / `parse_structured` の面を共有する(`LlmProvider` Protocol)。

**モデルとパラメータの既定について**:`claude-opus-5` に adaptive thinking を
使う。コスト都合でモデルを下げるのは人間の判断であり、コードの既定にはしない
(`config/collection.yaml` の `llm.model` / `llm.effort` で下げられる)。
長文(10-Kのリスク要因は数十万文字になる)を扱う経路は**必ずストリーミング**
にしている——非ストリーミングだと大きな `max_tokens` でHTTPタイムアウトに
当たるため。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import anthropic
from pydantic import BaseModel, ValidationError

from autoscreener.config import LlmConfig, load_llm_config
from autoscreener.llm.errors import (
    LlmDisabled,
    LlmInputTooLarge,
    LlmParseFailure,
    LlmRefusal,
    LlmTransientFailure,
    LlmTruncated,
    classify_exception,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmUsage:
    """1回の呼び出しのトークン内訳。

    `cache_read_tokens` を保存しているのは、**キャッシュが効いていないことに
    気づけるようにするため**。プロンプトキャッシュは前方一致で、システム
    プロンプトに日付やUUIDが1文字混ざるだけで黙って無効化される(エラーには
    ならず、ただ課金がおよそ10倍になる)。実測値をDBに残しておけば後から検算できる。
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> LlmUsage:
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True)
class LlmResult:
    """テキスト応答1件。`request_id` は障害報告用(SDKが `_request_id` で公開する)。"""

    text: str
    usage: LlmUsage
    model: str
    request_id: str | None = None


def guard_input_size(text: str, config: LlmConfig, *, label: str) -> str:
    """`llm.max_input_chars` を超えていたら `LlmInputTooLarge` を上げる。

    **切り詰めない。** 切られた本文の要約は、読んでいない箇所について何も
    言わないが、出力からはそれが分からない——「リスクは無かった」と
    「そこは読んでいない」が区別できない出力を作るくらいなら落とす。
    """
    if len(text) > config.max_input_chars:
        raise LlmInputTooLarge(
            f"{label}: {len(text):,}文字は上限 {config.max_input_chars:,} を超える。"
            "llm.max_input_chars を上げるか、入力を分割すること(切り詰めはしない)。",
            chars=len(text),
            limit=config.max_input_chars,
        )
    return text


def check_stop_reason(message: Any) -> None:
    """HTTP 200 で返ってくる2種類の失敗を例外に変える。

    `stop_reason` を見ずに `content` を読むと、拒否は空文字列に、打ち切りは
    「途中まで書かれた完成品に見える文章」になる。どちらも保存してはならない。
    Batch API の結果にも同じ検査が要るので公開関数にしてある。
    """
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        explanation = getattr(details, "explanation", None) if details is not None else None
        raise LlmRefusal(f"モデルが応答を拒否した (category={category}): {explanation}", category)
    if stop_reason == "max_tokens":
        raise LlmTruncated(
            "出力が max_tokens に当たって途中で切れた。"
            "llm.max_output_tokens を上げるか入力を分割すること(部分出力は保存しない)。"
        )


def text_of(message: Any) -> str:
    """`content` からテキストブロックだけを連結する。

    thinking ブロックが混ざりうるので `content[0].text` と決め打ちできない。
    """
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    text = "".join(parts).strip()
    if not text:
        raise LlmParseFailure("応答にテキストブロックが1つも無い")
    return text


class LlmClient:
    """Claude API への唯一の入口(`llm.provider = anthropic`)。

    `enabled=False` か APIキー未設定なら `from_config` の時点で `LlmDisabled`
    を上げる——呼び出し側が「LLM機能なし」として正常終了できるように、
    失敗を早く・1か所で出す(FRED未設定時と同じ扱い)。

    別プロバイダは `OpenAICompatClient`。両者を選ぶのは `build_provider()`。
    """

    provider_name = "anthropic"
    # Batch API(料金50%)は Anthropic 固有。`score_qualitative` はこの旗を見て、
    # False のプロバイダでは逐次呼び出しにフォールバックする。
    supports_batch = True

    def __init__(self, config: LlmConfig, api_key: str, *, client: Any | None = None) -> None:
        self.config = config
        # `client` を差し替えられるのはテストのため(実APIを叩かずに
        # プロンプト組み立てと応答解釈を検証する)。
        self._client = client if client is not None else anthropic.Anthropic(api_key=api_key)

    @classmethod
    def from_config(cls, config: LlmConfig | None = None) -> LlmClient:
        cfg = config or load_llm_config()
        if not cfg.enabled:
            raise LlmDisabled("config/collection.yaml の llm.enabled が false")
        # app_settings(UIから保存)→ .env の順で解決する。
        from autoscreener.runtime_settings import resolve_api_key

        api_key = resolve_api_key("anthropic")
        if not api_key or api_key == "CHANGE_ME":
            raise LlmDisabled(
                "Anthropic の APIキーが未設定(.env の ANTHROPIC_API_KEY または UI の"
                "LLM設定)。LLM機能(要約・定性サブスコア・レポート)は無効として扱う"
                "(他の機能はすべて動く)。"
            )
        return cls(cfg, api_key)

    # ------------------------------------------------------------------
    # 単発のテキスト生成(要約・レポート)
    # ------------------------------------------------------------------
    def complete_text(
        self,
        *,
        system: Sequence[dict[str, Any]],
        user: str,
        max_tokens: int | None = None,
    ) -> LlmResult:
        """ストリーミングでテキストを1件生成する。

        ストリーミングにするのは表示のためではなく、**大きな `max_tokens` で
        HTTPタイムアウトに当たらないため**。受け取り側は `get_final_message()`
        で完成品だけを見る。
        """
        try:
            with self._client.messages.stream(
                model=self.config.model,
                max_tokens=max_tokens or self.config.max_output_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.config.effort},
                system=list(system),
                messages=[{"role": "user", "content": user}],
            ) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 — 直後に分類して投げ直す
            raise classify_exception(exc) from exc

        check_stop_reason(message)
        return LlmResult(
            text=text_of(message),
            usage=LlmUsage.from_response(message.usage),
            model=message.model,
            request_id=getattr(message, "_request_id", None),
        )

    # ------------------------------------------------------------------
    # 構造化出力(定性サブスコア)
    # ------------------------------------------------------------------
    def parse_structured(
        self,
        *,
        system: Sequence[dict[str, Any]],
        user: str,
        output_model: type[BaseModel],
        max_tokens: int | None = None,
    ) -> tuple[Any, LlmUsage, str | None]:
        """`messages.parse` でPydanticモデルに検証済みの応答を得る。

        自前でJSONを切り出して `json.loads` するより、SDKの `parse` に任せる方が
        安全である(スキーマ違反はAPI側で防がれ、残った不整合はここで
        `LlmParseFailure` になる)。
        """
        try:
            response = self._client.messages.parse(
                model=self.config.model,
                max_tokens=max_tokens or self.config.max_output_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.config.effort},
                system=list(system),
                messages=[{"role": "user", "content": user}],
                output_format=output_model,
            )
        except ValidationError as exc:
            raise LlmParseFailure(f"構造化出力がスキーマに合わない: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from exc

        check_stop_reason(response)
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LlmParseFailure("parsed_output が None(構造化出力が返っていない)")
        return parsed, LlmUsage.from_response(response.usage), getattr(response, "_request_id", None)

    # ------------------------------------------------------------------
    # Batch API(銘柄数が多く、待てる処理)
    # ------------------------------------------------------------------
    def submit_batch(self, requests: list[Any]) -> str:
        """Batch API にまとめて投げ、batch_id を返す。

        定性サブスコアのように「数十〜数百銘柄・即時性不要」の処理はこちらを使う
        (料金が50%になる)。逆に1件だけのレポート生成にBatchを使う理由は無い。
        """
        try:
            batch = self._client.messages.batches.create(requests=requests)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from exc
        return batch.id

    def wait_for_batch(self, batch_id: str) -> Any:
        """`processing_status == "ended"` になるまで待つ。

        `llm.batch_timeout_seconds` を超えたら `LlmTransientFailure` にする。
        **バッチ自体はサーバ側で走り続ける**ので、ログに出した `batch_id` を
        控えておけば後から `collect_batch` で回収できる——ここで投げる例外は
        「待つのをやめた」であって「失われた」ではない。
        """
        deadline = time.monotonic() + self.config.batch_timeout_seconds
        while True:
            try:
                batch = self._client.messages.batches.retrieve(batch_id)
            except Exception as exc:  # noqa: BLE001
                raise classify_exception(exc) from exc
            if batch.processing_status == "ended":
                return batch
            if time.monotonic() > deadline:
                raise LlmTransientFailure(
                    f"batch {batch_id} が llm.batch_timeout_seconds 以内に終わらなかった。"
                    "サーバ側では処理が続いているので、この batch_id で後から回収できる。"
                )
            logger.info("batch %s: %s", batch_id, batch.processing_status)
            time.sleep(self.config.batch_poll_interval_seconds)

    def collect_batch(self, batch_id: str) -> Iterator[Any]:
        """結果を1件ずつ返す。**順序は保証されないので `custom_id` で引くこと。**"""
        try:
            yield from self._client.messages.batches.results(batch_id)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from exc


# ---------------------------------------------------------------------------
# OpenAI 互換プロバイダ(`llm.provider = openai_compat`)
# ---------------------------------------------------------------------------

_EFFORT_TO_REASONING = {
    # OpenAI の `reasoning_effort` は low|medium|high(gpt-5 系のみ minimal)。
    # このアプリの effort 尺度(xhigh|max を含む)を、通る値に丸める。
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


@runtime_checkable
class LlmProvider(Protocol):
    """`LlmClient` と `OpenAICompatClient` の共通面。バッチ処理は任意。"""

    provider_name: str
    supports_batch: bool

    def complete_text(
        self, *, system: Sequence[dict[str, Any]], user: str, max_tokens: int | None = None
    ) -> LlmResult: ...

    def parse_structured(
        self,
        *,
        system: Sequence[dict[str, Any]],
        user: str,
        output_model: type[BaseModel],
        max_tokens: int | None = None,
    ) -> tuple[Any, LlmUsage, str | None]: ...


class OpenAICompatClient:
    """OpenAI 互換 `/v1/chat/completions` への入口(ChatGPT / NVIDIA NIM /
    Ollama / vLLM / LM Studio / LiteLLM)。

    Anthropic 版と**同じメソッド面**(`complete_text` / `parse_structured`)を
    提供するが、`thinking` / `output_config.effort` / Batch API は無い。
    `effort` は既定では送らず(`llm.send_effort = true` のときだけ
    `reasoning_effort` にマップ)、互換サーバが未知パラメータを 400 で
    弾く事故を避ける。

    **`stop_reason` に相当する `finish_reason` の検査は Anthropic 版と同じく必須。**
    `"length"`(= `max_tokens` 打ち切り)と `"content_filter"`(= 拒否)は
    どちらも HTTP 200 で返り、それらしい本文が入っていることがある。
    """

    provider_name = "openai_compat"
    supports_batch = False

    def __init__(self, config: LlmConfig, api_key: str, *, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self._client = client
        else:
            import openai

            self._client = openai.OpenAI(api_key=api_key, base_url=config.base_url or None)

    @classmethod
    def from_config(cls, config: LlmConfig | None = None) -> OpenAICompatClient:
        cfg = config or load_llm_config()
        if not cfg.enabled:
            raise LlmDisabled("config/collection.yaml の llm.enabled が false")
        from autoscreener.runtime_settings import resolve_api_key

        api_key = resolve_api_key("openai_compat")
        if not api_key or api_key == "CHANGE_ME":
            raise LlmDisabled(
                "OpenAI互換プロバイダのAPIキーが未設定(.env の OPENAI_API_KEY または UI の"
                "LLM設定)。ローカルLLM(Ollama 等)なら任意のダミー文字列で良い。"
            )
        return cls(cfg, api_key)

    # ------------------------------------------------------------------
    def _messages(self, system: Sequence[dict[str, Any]], user: str) -> list[dict[str, str]]:
        """複数の system ブロックを1つの system メッセージに畳む。

        `cache_control` は OpenAI 側に無いので落とす。プロンプトの前方一致
        キャッシュは多くの互換サーバが自動で行うため、テキストの順序さえ
        安定していれば効く。
        """
        system_text = "\n\n".join(block["text"] for block in system if block.get("text"))
        msgs: list[dict[str, str]] = []
        if system_text:
            msgs.append({"role": "system", "content": system_text})
        msgs.append({"role": "user", "content": user})
        return msgs

    def _extra(self) -> dict[str, Any]:
        if not self.config.send_effort:
            return {}
        mapped = _EFFORT_TO_REASONING.get(self.config.effort)
        return {"reasoning_effort": mapped} if mapped else {}

    @staticmethod
    def _check_finish_reason(finish_reason: str | None) -> None:
        if finish_reason == "length":
            raise LlmTruncated(
                "出力が max_tokens に当たって途中で切れた。"
                "llm.max_output_tokens を上げるか入力を分割すること(部分出力は保存しない)。"
            )
        if finish_reason == "content_filter":
            raise LlmRefusal("プロバイダのコンテンツフィルタが応答を止めた", "content_filter")

    @staticmethod
    def _usage(raw: Any) -> LlmUsage:
        if raw is None:
            return LlmUsage()
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        return LlmUsage(
            input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
            cache_read_tokens=cached or 0,
            cache_creation_tokens=0,
            output_tokens=getattr(raw, "completion_tokens", 0) or 0,
        )

    # ------------------------------------------------------------------
    def complete_text(
        self,
        *,
        system: Sequence[dict[str, Any]],
        user: str,
        max_tokens: int | None = None,
    ) -> LlmResult:
        """ストリーミングでテキストを1件生成する(Anthropic 版と同じ理由——
        大きな `max_tokens` で HTTP タイムアウトに当たらないため)。"""
        chunks: list[str] = []
        finish_reason: str | None = None
        usage_raw: Any = None
        model_name = self.config.model
        request_id: str | None = None
        try:
            stream = self._client.chat.completions.create(
                model=self.config.model,
                max_tokens=max_tokens or self.config.max_output_tokens,
                messages=self._messages(system, user),
                stream=True,
                stream_options={"include_usage": True},
                **self._extra(),
            )
            for event in stream:
                request_id = getattr(event, "id", None) or request_id
                model_name = getattr(event, "model", None) or model_name
                if getattr(event, "usage", None) is not None:
                    usage_raw = event.usage
                for choice in getattr(event, "choices", []) or []:
                    delta = getattr(choice, "delta", None)
                    piece = getattr(delta, "content", None) if delta is not None else None
                    if piece:
                        chunks.append(piece)
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
        except Exception as exc:  # noqa: BLE001 — 直後に分類して投げ直す
            raise classify_exception(exc) from exc

        self._check_finish_reason(finish_reason)
        text = "".join(chunks).strip()
        if not text:
            raise LlmParseFailure("応答にテキストが1文字も無い")
        return LlmResult(
            text=text, usage=self._usage(usage_raw), model=model_name, request_id=request_id
        )

    # ------------------------------------------------------------------
    def parse_structured(
        self,
        *,
        system: Sequence[dict[str, Any]],
        user: str,
        output_model: type[BaseModel],
        max_tokens: int | None = None,
    ) -> tuple[Any, LlmUsage, str | None]:
        """`chat.completions.parse` で Pydantic モデルに検証済みの応答を得る。

        互換サーバが `response_format` の json_schema に未対応だと
        `LlmPermanentFailure`(400)になる——その場合はそのプロバイダでは
        `score-qualitative` を使えない(`generate-report` は影響しない)。
        """
        try:
            response = self._client.chat.completions.parse(
                model=self.config.model,
                max_tokens=max_tokens or self.config.max_output_tokens,
                messages=self._messages(system, user),
                response_format=output_model,
                **self._extra(),
            )
        except ValidationError as exc:
            raise LlmParseFailure(f"構造化出力がスキーマに合わない: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from exc

        choice = response.choices[0]
        self._check_finish_reason(getattr(choice, "finish_reason", None))
        message = choice.message
        if getattr(message, "refusal", None):
            raise LlmRefusal(f"モデルが応答を拒否した: {message.refusal}", None)
        parsed = getattr(message, "parsed", None)
        if parsed is None:
            raise LlmParseFailure("parsed が None(構造化出力が返っていない)")
        return parsed, self._usage(getattr(response, "usage", None)), getattr(response, "id", None)


def build_provider(config: LlmConfig | None = None) -> LlmProvider:
    """`llm.provider` に応じて Anthropic / OpenAI互換 のクライアントを作る。

    `enabled=False` か対応するAPIキー未設定なら `LlmDisabled`——呼び出し側は
    従来どおりこれを握って「LLM機能なし」で正常終了してよい。
    """
    cfg = config or load_llm_config()
    if cfg.provider == "openai_compat":
        return OpenAICompatClient.from_config(cfg)
    return LlmClient.from_config(cfg)
