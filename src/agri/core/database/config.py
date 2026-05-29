"""Database connection configuration for ``agri.core``.

The DSN is read lazily from the environment so that *importing*
``agri.core`` (and therefore any handler) never requires a live database
— unit tests and tooling import the package without a Postgres around.

Connection-string resolution order:

1. ``AGRI_DB_URL`` — the canonical variable.
2. ``DATABASE_URL`` — common fallback (Supabase / Heroku-style).

Whatever form the DSN arrives in, it is normalised to the psycopg 3
driver (``postgresql+psycopg://``) so SQLAlchemy always uses the modern
driver that ``agri-db`` ships.
"""

from __future__ import annotations

import os

_ENV_VARS = ("AGRI_DB_URL", "DATABASE_URL")


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when no database DSN is present in the environment.

    Deliberately raised at *connection* time, not import time, so the
    package stays importable without a configured database.
    """


def get_connection_string() -> str:
    """Return the normalised psycopg-3 DSN, or raise if none is set."""
    for var in _ENV_VARS:
        value = os.environ.get(var)
        if value:
            return normalize_dsn(value)
    raise DatabaseNotConfiguredError(
        "No database DSN found. Set AGRI_DB_URL (or DATABASE_URL) to the "
        "Agrilogy Postgres connection string."
    )


def normalize_dsn(dsn: str) -> str:
    """Force the psycopg-3 driver onto an otherwise driverless DSN.

    ``postgres://`` and ``postgresql://`` → ``postgresql+psycopg://``.
    A DSN that already names a driver (``postgresql+psycopg2://`` etc.)
    is left untouched.
    """
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    if dsn.startswith("postgresql://"):
        dsn = "postgresql+psycopg://" + dsn[len("postgresql://") :]
    return dsn
