"""agri.core.database — SQLAlchemy engine/session + the main DB client.

The DB-access layer for ``agri.core``, mirroring
``revly-core/src/revly/database/``. Handlers fetch through
:class:`AgriMainDBClient` over the ORM models that ``agri-db`` owns,
inside a :func:`session_scope`.

The engine is created lazily from ``AGRI_DB_URL`` on first use, so
importing this package (and the handlers above it) never needs a live
database.
"""

from __future__ import annotations

from agri.core.database.client import AgriMainDBClient
from agri.core.database.config import (
    DatabaseNotConfiguredError,
    get_connection_string,
    normalize_dsn,
)
from agri.core.database.session import (
    dispose_engine,
    get_engine,
    get_session,
    session_scope,
)

__all__ = [
    "AgriMainDBClient",
    "DatabaseNotConfiguredError",
    "dispose_engine",
    "get_connection_string",
    "get_engine",
    "get_session",
    "normalize_dsn",
    "session_scope",
]
