from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from autoscreener.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # S-8(2026-09-04、docs/daily_pipeline_throughput_plan_2026-09-04.md):
        # プールサイズを明示する。以前は引数を渡していなかったため、
        # SQLAlchemyの未宣言の既定値(pool_size=5, max_overflow=10 →
        # 合計15接続)にそのまま依存していた。値そのものは動いていたが、
        # 「なぜ15で足りているか」がどこにも書かれておらず、依存先の既定値が
        # 変わればサイレントに `pool_timeout` 例外に変わる状態だった。
        #
        # 15で足りていた理由は`config/collection.yaml`の2つの`max_workers`と
        # 直結している。`collection.max_workers`(S-8で5→10)は
        # `run_daily_collection`(collection)と`collect_consensus`
        # (S-4、同じ設定値を共有)の両方で使われ、ワーカー1本につきDB
        # セッション1本を保持しうる——ワーカー数10 + 呼び出し元スレッド分1 で
        # 最大約11接続。`edgar.max_workers`(S-5、10)を使うEDGAR系ステージ
        # (litigation/filing_sections/dilution/customer_concentration)も同水準。
        # ただし`daily_pipeline.py`はステージを**逐次**実行するため、実際に
        # 同時に生きるのはどれか1ステージぶん(≒11接続)だけであり、
        # 全ステージを足し合わせる必要は無い。
        #
        # 11に対して約3倍(pool_size=20 + max_overflow=10 = 合計30接続)の
        # 余裕を持たせた。今回のS-8のように、どちらかの`max_workers`を
        # さらに引き上げる変更が今後も起きうるため、変更のたびにこの数値も
        # 見直す前提で、限界ぎりぎりではなく明確な余白を残す。
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """商用グレード原則(18.3):書き込みは常にトランザクション境界を明示する。
    例外時はロールバックし、部分書き込みを残さない。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
