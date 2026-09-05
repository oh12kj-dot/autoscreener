import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoscreener.config import get_settings  # noqa: E402
from autoscreener.db.migration_guard import resolve_alembic_database_url  # noqa: E402
from autoscreener.db.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DB接続文字列は alembic.ini に重複させず、アプリの Settings(.env)から取得する。
#
# **2026-09-05 の事故**(docs/audit_followup_2026-09-05.md):
# `TEST_DATABASE_URL=...autoscreener_test uv run alembic upgrade head` を
# 「テストDBへ流すつもり」で実行したところ、alembicは `TEST_DATABASE_URL` を
# 一切見ないため(常に `get_settings().database_url` = `DATABASE_URL`/.env の
# 開発用DBを見る)、**サイレントに開発用DBへマイグレーションしてしまった**。
# そのときは実害なかったが、pytest向けにWP-A
# (docs/racr_wp_a_operational_safety_2026-09-04.md)が閉じたのと同じ種類の
# 事故クラス。`resolve_alembic_database_url`(autoscreener.db.migration_guard)
# が、`TEST_DATABASE_URL` が設定されているのに解決先が非テストDBのままという
# 「2つの合図が食い違う」状態だけを検出し、どちらかを黙って優先せず例外で
# 止める。
#
# 正しい実行方法:
#   開発用DBへ:  uv run alembic upgrade head
#   テストDBへ:  DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
#               (`TEST_DATABASE_URL` 単体では効かない -- 上記のとおり)
config.set_main_option(
    "sqlalchemy.url",
    resolve_alembic_database_url(
        get_settings().database_url, os.environ.get("TEST_DATABASE_URL")
    ),
)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
