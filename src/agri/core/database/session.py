"""Engine + session management for ``agri.core``.

Mirrors ``revly-core/src/revly/database/`` (``pgclient.py`` /
``db_service.py``): a single lazily-created engine cached at module
level, plus a ``session_scope()`` context manager that owns the
transaction boundary. Query helpers on ``AgriMainDBClient`` take the
``Session`` this yields, so callers control when work commits.

Engine settings are tuned for the Supabase transaction pooler:

* ``NullPool`` — the pooler (pgBouncer) owns connection pooling; a
  second pool in SQLAlchemy fights it. Each session checks out a fresh
  connection and returns it on close.
* ``prepare_threshold=None`` — pgBouncer in transaction mode cannot
  carry server-side prepared statements across pooled connections, so
  psycopg 3 must not create them.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from agri.core.database.config import get_connection_string

_engine: Engine | None = None
_session_factory: scoped_session[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_connection_string(),
            poolclass=NullPool,
            connect_args={"prepare_threshold": None},
            future=True,
        )
    return _engine


def _get_session_factory() -> scoped_session[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = scoped_session(
            sessionmaker(
                bind=get_engine(),
                autoflush=False,
                autocommit=False,
                future=True,
            )
        )
    return _session_factory


def get_session() -> Session:
    """Return a new ``Session`` from the shared factory.

    Prefer :func:`session_scope` for anything that mutates — it handles
    commit/rollback/close for you.
    """
    return _get_session_factory()()


@contextmanager
def session_scope(*, commit: bool = False) -> Generator[Session]:
    """Transactional scope around a series of operations.

    Rolls back on any exception, commits only when ``commit=True``, and
    always closes the session.
    """
    session = get_session()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Dispose the cached engine + factory. Mainly for tests / shutdown."""
    global _engine, _session_factory
    if _session_factory is not None:
        _session_factory.remove()
        _session_factory = None
    if _engine is not None:
        _engine.dispose()
        _engine = None
