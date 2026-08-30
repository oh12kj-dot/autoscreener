"""tests/unit/test_runtime_settings.py(K-9)。DBにもネットワークにも触れない。

アクティブな接続プロファイル(`llm_connections` の `is_active` 行)を
`active=` で直接渡し、collection.yaml / .env の上への重ね方だけを固定する。
押さえたいのは「アクティブが無ければ元の設定インスタンスをそのまま返す」ことと、
`model` / `effort` が空ならフォールバックすること、APIキーの解決順である。
"""

from __future__ import annotations

import pytest

from autoscreener.config import LlmConfig
from autoscreener.runtime_settings import (
    ActiveConnection,
    resolve_api_key,
    resolve_llm_config,
    secret_is_set,
)

_BASE = LlmConfig(provider="anthropic", model="claude-opus-5", effort="high")


def _conn(**over) -> ActiveConnection:
    defaults = dict(
        id=1,
        name="test",
        provider="anthropic",
        base_url=None,
        model=None,
        effort=None,
        send_effort=False,
        api_key=None,
    )
    defaults.update(over)
    return ActiveConnection(**defaults)


def test_no_active_connection_returns_the_same_instance():
    assert resolve_llm_config(_BASE, active=None) is _BASE


def test_active_connection_is_layered_over_the_base():
    cfg = resolve_llm_config(
        _BASE,
        active=_conn(
            provider="openai_compat",
            base_url="http://localhost:11434/v1",
            model="llama3.1:70b",
            effort="medium",
        ),
    )
    assert cfg.provider == "openai_compat"
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.model == "llama3.1:70b"
    assert cfg.effort == "medium"
    assert _BASE.provider == "anthropic"  # 元は不変


def test_empty_model_and_effort_fall_back_to_the_yaml_defaults():
    cfg = resolve_llm_config(_BASE, active=_conn(provider="openai_compat", model="", effort=None))
    assert cfg.model == "claude-opus-5"  # yaml の既定
    assert cfg.effort == "high"


def test_send_effort_is_carried_from_the_connection():
    assert resolve_llm_config(_BASE, active=_conn(send_effort=True)).send_effort is True


def test_invalid_provider_raises_through_the_validator():
    with pytest.raises(Exception):
        resolve_llm_config(_BASE, active=_conn(provider="bogus"))


def test_resolve_api_key_uses_the_connection_only_when_the_provider_matches():
    conn = _conn(provider="openai_compat", api_key="sk-db")
    assert resolve_api_key("openai_compat", active=conn) == "sk-db"
    # provider が違うプロファイルのキーは使わない(→ .env フォールバック)。
    assert resolve_api_key("anthropic", active=conn) != "sk-db"


def test_secret_is_set_is_false_for_placeholder_key():
    assert secret_is_set("anthropic", active=_conn(api_key="CHANGE_ME")) is False
    assert secret_is_set("anthropic", active=_conn(api_key="sk-real")) is True
