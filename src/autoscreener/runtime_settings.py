"""アクティブな名前付き接続プロファイルを、コミット済みの既定値の上に重ねる(K-9)。

`docs/ui_llm_provider_selection_2026-08-30.md`。`llm_connections`(DB)に
`is_active = true` の行があれば、`config/collection.yaml` / `.env` の上に
その provider / base_url / model / effort / send_effort / api_key を重ねる。
**アクティブが無い・テーブルが無い・DBに繋がらない、はすべて「上書き無し」
として静かに扱う**——CLIや単体テストが `llm_connections` を要求しないため。

**ここで解決する値はゲートにもスコアにも影響しない。** LLM の接続先とモデルを
選ぶだけで、`llm_analyses` への隔離はそのまま。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from autoscreener.config import LlmConfig, get_settings, load_llm_config

logger = logging.getLogger(__name__)


class _Unset:
    """`active` 未指定の番兵。`None`(アクティブ無し)と区別するため。"""


_UNSET = _Unset()

# アクティブ行から LlmConfig に写すフィールド。
_OVERLAY_FIELDS: tuple[str, ...] = ("provider", "base_url", "model", "effort", "send_effort")


@dataclass(frozen=True)
class ActiveConnection:
    """アクティブなプロファイルの実体。`api_key` の本体は API に渡さない。"""

    id: int
    name: str
    provider: str
    base_url: str | None
    model: str | None
    effort: str | None
    send_effort: bool
    api_key: str | None


def get_active_connection() -> ActiveConnection | None:
    """`llm_connections` の `is_active` 行を返す。DBに触れない環境では None。"""
    try:
        from autoscreener.db.models import LlmConnection
        from autoscreener.db.session import session_scope

        with session_scope() as session:
            row = session.query(LlmConnection).filter(LlmConnection.is_active.is_(True)).first()
            if row is None:
                return None
            return ActiveConnection(
                id=row.id,
                name=row.name,
                provider=row.provider,
                base_url=row.base_url,
                model=row.model,
                effort=row.effort,
                send_effort=bool(row.send_effort),
                api_key=row.api_key,
            )
    except Exception as exc:  # noqa: BLE001 — DB無し/テーブル無しは「上書き無し」
        logger.debug("llm_connections を読めなかった(上書き無しとして続行): %s", exc)
        return None


def _active(passed: object) -> ActiveConnection | None:
    """`active` 引数を解決する。`_UNSET` なら DB から取り直し、それ以外はそのまま。"""
    return get_active_connection() if passed is _UNSET else passed  # type: ignore[return-value]


def resolve_llm_config(base: LlmConfig | None = None, *, active: object = _UNSET) -> LlmConfig:
    """`load_llm_config()` にアクティブなプロファイルを重ねた設定を返す。

    上書きが1件も無ければ `base` をそのまま返す(新インスタンスを作らない)。
    `active` を明示的に渡すと再取得しない(`None` は「アクティブ無し」の意味)。
    """
    cfg = base or load_llm_config()
    conn = _active(active)
    if conn is None:
        return cfg

    overrides: dict[str, object] = {}
    for field in _OVERLAY_FIELDS:
        value = getattr(conn, field)
        if field == "send_effort":
            overrides[field] = bool(value)
        elif value is None or value == "":
            continue
        else:
            overrides[field] = value

    if not overrides:
        return cfg
    return type(cfg)(**{**cfg.model_dump(), **overrides})


def resolve_api_key(provider: str, *, active: object = _UNSET) -> str | None:
    """プロバイダのAPIキー。アクティブなプロファイルの provider が一致すれば
    その `api_key` を、さもなくば `.env` を使う。"""
    conn = _active(active)
    if conn is not None and conn.provider == provider and conn.api_key:
        return conn.api_key
    env = get_settings()
    return env.openai_api_key if provider == "openai_compat" else env.anthropic_api_key


def secret_is_set(provider: str, *, active: object = _UNSET) -> bool:
    """APIキーが(プロファイルか .env の少なくとも一方に)設定されているか。本体は返さない。"""
    key = resolve_api_key(provider, active=active)
    return bool(key) and key != "CHANGE_ME"
