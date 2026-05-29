"""Tests for the agri.core.database DB layer.

These run with NO live database: the engine is lazy, so we exercise DSN
normalisation, the not-configured error, and the lazy-engine contract
without ever connecting.
"""

from __future__ import annotations

import pytest

from agri.core import database
from agri.core.database import (
    AgriMainDBClient,
    DatabaseNotConfiguredError,
    get_connection_string,
    normalize_dsn,
)
from agri.core.database import session as session_mod


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        # Already-driven DSNs are left untouched.
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ],
)
def test_normalize_dsn(raw: str, expected: str) -> None:
    assert normalize_dsn(raw) == expected


def test_get_connection_string_prefers_agri_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGRI_DB_URL", "postgres://a@h/x")
    monkeypatch.setenv("DATABASE_URL", "postgres://b@h/y")
    assert get_connection_string() == "postgresql+psycopg://a@h/x"


def test_get_connection_string_falls_back_to_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGRI_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://b@h/y")
    assert get_connection_string() == "postgresql+psycopg://b@h/y"


def test_get_connection_string_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGRI_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseNotConfiguredError):
        get_connection_string()


def test_engine_is_lazy_no_db_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the package must not create an engine or hit a DB."""
    monkeypatch.delenv("AGRI_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    session_mod.dispose_engine()
    # No DSN configured, yet import + module access already succeeded above.
    # Asking for the engine now is what surfaces the missing config.
    with pytest.raises(DatabaseNotConfiguredError):
        session_mod.get_engine()


def test_public_surface() -> None:
    for name in ("AgriMainDBClient", "session_scope", "get_session", "get_engine"):
        assert hasattr(database, name)
    # The client exposes its generic primitives.
    for helper in ("get", "get_one", "list_", "exists"):
        assert hasattr(AgriMainDBClient, helper)
