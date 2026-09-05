"""Defect 1 (2026-09-05 audit, docs/audit_followup_2026-09-05.md): alembic
had no test-database isolation. ``alembic/env.py`` resolved
``sqlalchemy.url`` straight from ``get_settings().database_url`` and never
looked at ``TEST_DATABASE_URL`` at all -- so a session that ran
``TEST_DATABASE_URL=...autoscreener_test uv run alembic upgrade head``
intending to migrate the test database instead silently migrated the dev
database (``get_settings().database_url`` is unaffected by
``TEST_DATABASE_URL``).

These tests exercise ``autoscreener.db.migration_guard
.resolve_alembic_database_url`` -- the pure function ``alembic/env.py`` now
routes through -- plus a wiring check that ``alembic/env.py`` still calls
it (so a future edit cannot quietly revert to the unguarded
``get_settings().database_url`` assignment).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoscreener.db.migration_guard import (
    AmbiguousMigrationTargetError,
    resolve_alembic_database_url,
)

_DEV_URL = "postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener"
_TEST_URL = "postgresql+psycopg://autoscreener:autoscreener@localhost:5432/autoscreener_test"
_OTHER_PROD_LIKE_URL = "postgresql+psycopg://autoscreener:autoscreener@db.prod.internal:5432/autoscreener"


def test_test_database_url_unset_passes_dev_url_through_unchanged():
    """The overwhelmingly common case: no TEST_DATABASE_URL in the
    environment at all -- alembic must keep working exactly as before."""
    assert resolve_alembic_database_url(_DEV_URL, None) == _DEV_URL


def test_test_database_url_unset_passes_test_url_through_unchanged():
    assert resolve_alembic_database_url(_TEST_URL, None) == _TEST_URL


def test_test_database_url_consistent_with_resolved_test_url_is_not_ambiguous():
    """DATABASE_URL was explicitly pointed at the test DB (the documented
    correct invocation) while TEST_DATABASE_URL also happens to be set to
    the same database -- no conflict, must not raise."""
    assert resolve_alembic_database_url(_TEST_URL, _TEST_URL) == _TEST_URL


def test_reproduces_the_2026_09_05_incident_and_now_refuses():
    """This is the exact incident: TEST_DATABASE_URL set (intending to
    migrate the test DB) but the resolved DATABASE_URL still names the dev
    database -- alembic must refuse instead of silently migrating dev."""
    with pytest.raises(AmbiguousMigrationTargetError):
        resolve_alembic_database_url(_DEV_URL, _TEST_URL)


def test_refuses_for_any_non_test_resolved_database_not_only_the_dev_default():
    with pytest.raises(AmbiguousMigrationTargetError):
        resolve_alembic_database_url(_OTHER_PROD_LIKE_URL, _TEST_URL)


def test_error_message_names_both_databases_and_the_correct_invocation():
    with pytest.raises(AmbiguousMigrationTargetError) as exc_info:
        resolve_alembic_database_url(_DEV_URL, _TEST_URL)
    message = str(exc_info.value)
    assert "autoscreener_test" in message
    assert "autoscreener" in message
    assert "DATABASE_URL=$TEST_DATABASE_URL" in message


def test_alembic_env_still_routes_through_the_guard():
    """Wiring guard: fails if a future edit to alembic/env.py reintroduces
    the 2026-09-05 hazard by setting sqlalchemy.url straight from
    get_settings().database_url without going through
    resolve_alembic_database_url."""
    env_py = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    text = env_py.read_text(encoding="utf-8")
    assert "resolve_alembic_database_url" in text
    assert 'os.environ.get("TEST_DATABASE_URL")' in text
