"""tests/unit/test_llm_client.py(K-9)。実APIは叩かない(課金しない)。

押さえているのは、**HTTP 200 で返ってくる失敗**である。拒否
(`stop_reason == "refusal"`)は空文字列に、打ち切り(`max_tokens`)は
「完成品に見える途中の文章」になって返る。どちらも例外にならないので、
検査を外しても既存のテストは全部通ってしまう——だからここで固定する。
"""

from __future__ import annotations

import anthropic
import pytest

from autoscreener.config import LlmConfig
from autoscreener.llm.client import (
    LlmClient,
    LlmUsage,
    OpenAICompatClient,
    build_provider,
    guard_input_size,
)
from autoscreener.llm.errors import (
    LlmInputTooLarge,
    LlmParseFailure,
    LlmPermanentFailure,
    LlmRefusal,
    LlmTransientFailure,
    LlmTruncated,
    classify_exception,
)

_CONFIG = LlmConfig(max_input_chars=100, max_output_tokens=1000)


class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class _Usage:
    def __init__(self, **kwargs) -> None:
        self.input_tokens = kwargs.get("input_tokens", 0)
        self.cache_read_input_tokens = kwargs.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = kwargs.get("cache_creation_input_tokens", 0)
        self.output_tokens = kwargs.get("output_tokens", 0)


class _StopDetails:
    def __init__(self, category: str, explanation: str) -> None:
        self.category = category
        self.explanation = explanation


class _Message:
    def __init__(self, content, stop_reason="end_turn", stop_details=None, usage=None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = usage or _Usage()
        self.model = "claude-opus-5"
        self._request_id = "req_test"


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self) -> _Message:
        return self._message


class _Messages:
    """`client.messages` の代役。呼び出し時のkwargsを記録する。"""

    def __init__(self, message: _Message) -> None:
        self._message = message
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self._message)


class _FakeClient:
    def __init__(self, message: _Message) -> None:
        self.messages = _Messages(message)


def _client(message: _Message, config: LlmConfig | None = None) -> LlmClient:
    return LlmClient(config or _CONFIG, "unused", client=_FakeClient(message))


# ---------------------------------------------------------------------------
# HTTP 200 で返る失敗
# ---------------------------------------------------------------------------


def test_refusal_raises_instead_of_returning_empty_text():
    message = _Message([], stop_reason="refusal", stop_details=_StopDetails("cyber", "no"))
    with pytest.raises(LlmRefusal) as excinfo:
        _client(message).complete_text(system=[], user="x")
    assert excinfo.value.category == "cyber"


def test_truncated_output_is_not_returned_as_a_finished_summary():
    """`max_tokens` 打ち切りは、それらしい文章が入っていても保存させない。"""
    message = _Message([_Block("text", "要旨\nこの会社は")], stop_reason="max_tokens")
    with pytest.raises(LlmTruncated):
        _client(message).complete_text(system=[], user="x")


def test_response_without_a_text_block_is_a_parse_failure():
    """thinking しか無い応答を空文字列として通さない。"""
    message = _Message([_Block("thinking")])
    with pytest.raises(LlmParseFailure):
        _client(message).complete_text(system=[], user="x")


def test_text_blocks_are_concatenated_and_thinking_is_skipped():
    message = _Message([_Block("thinking", "内部"), _Block("text", "A"), _Block("text", "B")])
    assert _client(message).complete_text(system=[], user="x").text == "AB"


# ---------------------------------------------------------------------------
# 呼び出しパラメータ
# ---------------------------------------------------------------------------


def test_request_uses_adaptive_thinking_and_configured_effort():
    message = _Message([_Block("text", "ok")])
    client = _client(message, LlmConfig(effort="xhigh", max_output_tokens=1234))
    client.complete_text(system=[{"type": "text", "text": "s"}], user="u")
    call = client._client.messages.calls[0]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "xhigh"}
    assert call["max_tokens"] == 1234
    assert call["model"] == "claude-opus-5"


# ---------------------------------------------------------------------------
# 入力サイズ:切り詰めずに落とす
# ---------------------------------------------------------------------------


def test_oversized_input_fails_instead_of_being_truncated():
    with pytest.raises(LlmInputTooLarge) as excinfo:
        guard_input_size("x" * 101, _CONFIG, label="ZZ 10-K")
    assert excinfo.value.chars == 101
    assert excinfo.value.limit == 100


def test_input_at_the_limit_passes_through_unchanged():
    text = "x" * 100
    assert guard_input_size(text, _CONFIG, label="ZZ") == text


# ---------------------------------------------------------------------------
# 使用トークンの取り出し
# ---------------------------------------------------------------------------


def test_usage_records_cache_reads():
    """キャッシュが効いているかを後から検算できるように保存する。"""
    usage = LlmUsage.from_response(
        _Usage(input_tokens=10, cache_read_input_tokens=900, output_tokens=5)
    )
    assert usage.as_dict() == {
        "input_tokens": 10,
        "cache_read_tokens": 900,
        "cache_creation_tokens": 0,
        "output_tokens": 5,
    }


# ---------------------------------------------------------------------------
# 例外の分類:リトライすべきものとそうでないものを取り違えない
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers = {}
        self.request = None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (anthropic.AuthenticationError("bad key", response=_Resp(401), body=None), LlmPermanentFailure),
        (anthropic.BadRequestError("bad", response=_Resp(400), body=None), LlmPermanentFailure),
        (anthropic.NotFoundError("no model", response=_Resp(404), body=None), LlmPermanentFailure),
        (anthropic.RateLimitError("slow down", response=_Resp(429), body=None), LlmTransientFailure),
        (anthropic.InternalServerError("boom", response=_Resp(500), body=None), LlmTransientFailure),
    ],
)
def test_sdk_exceptions_map_onto_the_taxonomy(exc, expected):
    assert isinstance(classify_exception(exc), expected)


def test_unknown_exceptions_become_parse_failures_not_transient():
    """分類できない失敗を「一時的」として黙ってリトライしないこと。

    `collectors/errors.py` と同じ方針——未知の失敗はAPIかSDKの契約が変わった
    合図なので、リトライで隠さず表に出す。
    """
    assert isinstance(classify_exception(RuntimeError("???")), LlmParseFailure)


def test_llm_errors_pass_through_classification_unchanged():
    original = LlmTruncated("already classified")
    assert classify_exception(original) is original


# ---------------------------------------------------------------------------
# OpenAI 互換プロバイダ(llm.provider = openai_compat)
#
# Anthropic 版と同じく、HTTP 200 で返る失敗(finish_reason == "length" /
# "content_filter")を保存させないことを固定する。
# ---------------------------------------------------------------------------


class _OAChoiceDelta:
    def __init__(self, content: str | None = None, finish_reason: str | None = None) -> None:
        self.delta = type("D", (), {"content": content})()
        self.finish_reason = finish_reason


class _OAChunk:
    def __init__(self, choices, usage=None, model="gpt-5", id_="chatcmpl-x") -> None:
        self.choices = choices
        self.usage = usage
        self.model = model
        self.id = id_


class _OAUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0, cached=0) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = type("PT", (), {"cached_tokens": cached})()


class _OACompletions:
    def __init__(self, chunks=None, parse_response=None) -> None:
        self._chunks = chunks or []
        self._parse_response = parse_response
        self.create_calls: list[dict] = []
        self.parse_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return iter(self._chunks)

    def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        return self._parse_response


class _FakeOpenAI:
    def __init__(self, chunks=None, parse_response=None) -> None:
        self.chat = type(
            "Chat", (), {"completions": _OACompletions(chunks, parse_response)}
        )()


def _oa_client(chunks=None, parse_response=None, config: LlmConfig | None = None):
    cfg = config or LlmConfig(provider="openai_compat", model="gpt-5", max_output_tokens=1000)
    return OpenAICompatClient(cfg, "unused", client=_FakeOpenAI(chunks, parse_response))


def test_openai_compat_concatenates_streamed_chunks():
    chunks = [
        _OAChunk([_OAChoiceDelta("要旨\n")]),
        _OAChunk([_OAChoiceDelta("この会社は半導体を売る。", finish_reason="stop")]),
        _OAChunk([], usage=_OAUsage(prompt_tokens=12, completion_tokens=8, cached=4)),
    ]
    result = _oa_client(chunks).complete_text(system=[{"type": "text", "text": "s"}], user="u")
    assert result.text == "要旨\nこの会社は半導体を売る。"
    assert result.usage.input_tokens == 12
    assert result.usage.cache_read_tokens == 4
    assert result.usage.output_tokens == 8


def test_openai_compat_length_finish_reason_is_truncation():
    chunks = [_OAChunk([_OAChoiceDelta("途中まで書い", finish_reason="length")])]
    with pytest.raises(LlmTruncated):
        _oa_client(chunks).complete_text(system=[], user="u")


def test_openai_compat_content_filter_is_a_refusal():
    chunks = [_OAChunk([_OAChoiceDelta("", finish_reason="content_filter")])]
    with pytest.raises(LlmRefusal):
        _oa_client(chunks).complete_text(system=[], user="u")


def test_openai_compat_empty_response_is_a_parse_failure():
    chunks = [_OAChunk([_OAChoiceDelta("", finish_reason="stop")])]
    with pytest.raises(LlmParseFailure):
        _oa_client(chunks).complete_text(system=[], user="u")


def test_openai_compat_does_not_send_effort_by_default():
    """互換サーバの多くは未知パラメータ(reasoning_effort)を 400 で弾く。"""
    chunks = [_OAChunk([_OAChoiceDelta("ok", finish_reason="stop")])]
    client = _oa_client(chunks)
    client.complete_text(system=[], user="u")
    call = client._client.chat.completions.create_calls[0]
    assert "reasoning_effort" not in call
    assert call["stream"] is True


def test_openai_compat_sends_effort_when_enabled_and_maps_xhigh_to_high():
    chunks = [_OAChunk([_OAChoiceDelta("ok", finish_reason="stop")])]
    cfg = LlmConfig(
        provider="openai_compat", model="o4-mini", effort="xhigh", send_effort=True
    )
    client = OpenAICompatClient(cfg, "unused", client=_FakeOpenAI(chunks))
    client.complete_text(system=[], user="u")
    assert client._client.chat.completions.create_calls[0]["reasoning_effort"] == "high"


def test_openai_compat_parse_structured_returns_validated_model():
    from pydantic import BaseModel

    class _Out(BaseModel):
        verdict: str

    message = type("M", (), {"parsed": _Out(verdict="ok"), "refusal": None})()
    choice = type("C", (), {"message": message, "finish_reason": "stop"})()
    response = type("R", (), {"choices": [choice], "usage": _OAUsage(3, 2), "id": "r1"})()

    parsed, usage, req_id = _oa_client(parse_response=response).parse_structured(
        system=[], user="u", output_model=_Out
    )
    assert parsed.verdict == "ok"
    assert usage.output_tokens == 2
    assert req_id == "r1"


def test_openai_compat_parse_structured_refusal_raises():
    from pydantic import BaseModel

    class _Out(BaseModel):
        verdict: str

    message = type("M", (), {"parsed": None, "refusal": "no"})()
    choice = type("C", (), {"message": message, "finish_reason": "stop"})()
    response = type("R", (), {"choices": [choice], "usage": None, "id": "r1"})()
    with pytest.raises(LlmRefusal):
        _oa_client(parse_response=response).parse_structured(
            system=[], user="u", output_model=_Out
        )


def test_openai_compat_reports_no_batch_support():
    assert _oa_client().supports_batch is False
    assert _oa_client().provider_name == "openai_compat"


def test_build_provider_dispatches_on_config(monkeypatch):
    import autoscreener.llm.client as client_mod

    monkeypatch.setattr(
        client_mod.LlmClient, "from_config", classmethod(lambda cls, cfg=None: "ANTHROPIC")
    )
    monkeypatch.setattr(
        client_mod.OpenAICompatClient,
        "from_config",
        classmethod(lambda cls, cfg=None: "OPENAI"),
    )
    assert build_provider(LlmConfig(provider="anthropic")) == "ANTHROPIC"
    assert build_provider(LlmConfig(provider="openai_compat")) == "OPENAI"
