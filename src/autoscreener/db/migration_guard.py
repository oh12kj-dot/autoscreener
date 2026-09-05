"""Guard against alembic silently migrating the wrong database.

2026-09-05 incident (docs/audit_followup_2026-09-05.md): a session ran

    TEST_DATABASE_URL=postgresql+psycopg://...autoscreener_test uv run alembic upgrade head

intending to migrate the dedicated test database. ``alembic/env.py`` does
not read ``TEST_DATABASE_URL`` at all -- it resolves the migration target
the same way the app does, via ``autoscreener.config.get_settings()
.database_url`` (the ``DATABASE_URL`` env var, else the dev-DB default from
``.env``). Because ``DATABASE_URL`` was not also set, the command silently
migrated the *dev* database instead. It was harmless that time, but it is
the same class of hazard ``tests/conftest.py``'s
``_require_isolated_test_database`` (WP-A, docs/racr_wp_a_operational
_safety_2026-09-04.md) already closed for pytest -- pytest fails closed
when it cannot prove it is pointed at a database named ``*_autoscreener_test``;
alembic had no equivalent guard.

This module is the guard: a small, pure, unit-testable function that
``alembic/env.py`` calls before setting ``sqlalchemy.url``. It never
silently prefers one signal over the other -- if ``TEST_DATABASE_URL`` is
set (signalling "I mean to touch the test database") but the URL alembic
would actually use is *not* a test database, it refuses loudly instead of
guessing.

Correct invocations::

    # Dev database (the default -- do not set TEST_DATABASE_URL for this):
    uv run alembic upgrade head

    # Test database (explicit opt-in: point DATABASE_URL itself at the
    # test DB for this invocation; alembic never reads TEST_DATABASE_URL):
    DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
"""

from __future__ import annotations

from urllib.parse import urlsplit

_TEST_DB_SUFFIX = "autoscreener_test"


class AmbiguousMigrationTargetError(RuntimeError):
    """Raised when TEST_DATABASE_URL is set but alembic would migrate a
    database that does not look like the test database.

    Deliberately a plain, loud exception (propagates out of ``env.py`` and
    aborts the alembic process with a nonzero exit code) rather than a
    warning -- the entire point is that this class of mistake must never
    again pass silently.
    """


def _db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def resolve_alembic_database_url(
    resolved_url: str, test_database_url: str | None
) -> str:
    """Return the URL alembic should migrate, refusing on an unresolved conflict.

    Args:
        resolved_url: whatever ``get_settings().database_url`` currently
            returns -- this is what alembic would use unconditionally
            before this guard existed.
        test_database_url: ``os.environ.get("TEST_DATABASE_URL")``, i.e.
            whether the caller has signalled test-database intent.

    Returns ``resolved_url`` unchanged whenever there is no ambiguity:
    ``TEST_DATABASE_URL`` unset (the normal dev-migration path), or set but
    consistent with ``resolved_url`` already naming a ``*autoscreener_test``
    database. Raises ``AmbiguousMigrationTargetError`` only when
    ``TEST_DATABASE_URL`` is set *and* ``resolved_url`` names a database
    that does not look like the test database -- the exact shape of the
    2026-09-05 incident.
    """
    if not test_database_url:
        return resolved_url

    resolved_name = _db_name(resolved_url)
    if resolved_name.endswith(_TEST_DB_SUFFIX):
        return resolved_url

    test_name = _db_name(test_database_url)
    raise AmbiguousMigrationTargetError(
        "alembic refused to run: TEST_DATABASE_URL is set (it targets "
        f"database {test_name!r}), but alembic would migrate database "
        f"{resolved_name!r} -- which does not look like a test database "
        f"(does not end in {_TEST_DB_SUFFIX!r}).\n"
        "alembic does NOT read TEST_DATABASE_URL -- it always resolves the "
        "migration target the same way the app does "
        "(autoscreener.config.get_settings().database_url, i.e. the "
        "DATABASE_URL env var or .env). This is the exact silent-wrong-"
        "database hazard from the 2026-09-05 incident "
        "(docs/audit_followup_2026-09-05.md): TEST_DATABASE_URL was set "
        "intending to migrate the test database, but alembic silently "
        "migrated the dev database instead.\n"
        "  To migrate the TEST database:\n"
        "    DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head\n"
        "  To migrate the DEV database, unset TEST_DATABASE_URL first (a "
        "value left over from a previous `pytest` invocation in the same "
        "shell is enough to trip this check):\n"
        "    unset TEST_DATABASE_URL  # or: Remove-Item Env:TEST_DATABASE_URL\n"
        "    uv run alembic upgrade head"
    )
