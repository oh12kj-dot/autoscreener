"""APIレイヤーのDB依存関係。

18.6:バッチ層(書き込み)とAPI層(読み取り専用)でDBユーザーを分離する。
`api_database_url`(未設定なら`database_url`にフォールバック)で接続する
専用のエンジン・セッションファクトリを、バッチ層(`db.session`)とは
別に保持する。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from autoscreener.config import get_settings

_api_engine: Engine | None = None
_ApiSessionFactory: sessionmaker[Session] | None = None


def _get_api_engine() -> Engine:
    global _api_engine
    if _api_engine is None:
        settings = get_settings()
        url = settings.api_database_url or settings.database_url
        _api_engine = create_engine(url, pool_pre_ping=True)
    return _api_engine


def _get_api_session_factory() -> sessionmaker[Session]:
    global _ApiSessionFactory
    if _ApiSessionFactory is None:
        _ApiSessionFactory = sessionmaker(bind=_get_api_engine(), expire_on_commit=False)
    return _ApiSessionFactory


def get_session() -> Iterator[Session]:
    session = _get_api_session_factory()()
    try:
        yield session
    finally:
        session.close()
