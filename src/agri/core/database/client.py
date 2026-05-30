"""The canonical DB accessor for ``agri.core``.

Mirrors ``revly-core``'s ``RevlyMainDBClient``: query helpers are
``@staticmethod`` and take an externally-managed ``Session`` (from
:func:`agri.core.database.session.session_scope`), so the caller owns
the transaction boundary and a single session can serve several reads.

This module ships only the *generic* primitives every handler builds
on (``get`` / ``get_one`` / ``list_`` / ``exists``). Domain-specific
helpers — sensor reads, zone lookups, alert specs — land in the
handler-migration PR, each as a ``@staticmethod`` over the SQLAlchemy
models in ``agri.db``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from agri.db.base import AgriBase

ModelT = TypeVar("ModelT", bound=AgriBase)


class AgriMainDBClient:
    """Static accessor for the Agrilogy main Postgres (the agri-db schema)."""

    @staticmethod
    def get(session: Session, model: type[ModelT], pk: object) -> ModelT | None:
        """Fetch one row by primary key, or ``None``."""
        return session.get(model, pk)

    @staticmethod
    def get_one(
        session: Session, model: type[ModelT], *criteria: ColumnElement[bool]
    ) -> ModelT | None:
        """Fetch the single row matching ``criteria``, or ``None``.

        Raises if more than one row matches — use :meth:`list_` when
        multiple rows are expected.
        """
        return session.scalars(select(model).where(*criteria)).one_or_none()

    @staticmethod
    def list_(
        session: Session, model: type[ModelT], *criteria: ColumnElement[bool]
    ) -> list[ModelT]:
        """Return every row matching ``criteria`` (all rows if none given)."""
        return list(session.scalars(select(model).where(*criteria)).all())

    @staticmethod
    def exists(session: Session, model: type[ModelT], *criteria: ColumnElement[bool]) -> bool:
        """``True`` if at least one row matches ``criteria``."""
        return session.scalars(select(model).where(*criteria).limit(1)).first() is not None

    # --- sensor-reading helpers (every agri.db reading model has
    # value / zone_id / timestamp columns) ---------------------------------

    @staticmethod
    def average_value(
        session: Session,
        model: type[ModelT],
        *,
        zone_id: int,
        start: datetime,
        end: datetime,
    ) -> float | None:
        """Mean of ``model.value`` over ``[start, end)`` for ``zone_id``.

        Mirrors the Django ``_avg`` adapter helper: SQL ``AVG`` ignores
        NULL values and yields ``NULL`` (→ ``None``) when no rows match.
        ``model`` must expose ``value`` / ``zone_id`` / ``timestamp``
        columns, which every sensor-reading model in ``agri.db`` does.
        """
        return session.scalar(
            select(func.avg(model.value)).where(
                model.zone_id == zone_id,
                model.timestamp >= start,
                model.timestamp < end,
            )
        )

    @staticmethod
    def sum_value(
        session: Session,
        model: type[ModelT],
        *,
        zone_id: int,
        start: datetime,
        end: datetime,
    ) -> float | None:
        """Sum of ``model.value`` over ``[start, end)`` for ``zone_id``.

        ``None`` when no rows match (SQL ``SUM`` of an empty set).
        """
        return session.scalar(
            select(func.sum(model.value)).where(
                model.zone_id == zone_id,
                model.timestamp >= start,
                model.timestamp < end,
            )
        )

    @staticmethod
    def latest(
        session: Session, model: type[ModelT], *criteria: ColumnElement[bool]
    ) -> ModelT | None:
        """Most recent row (by ``timestamp`` desc) matching ``criteria``, or ``None``.

        Mirrors the Django ``_latest`` adapter helper.
        """
        return session.scalars(
            select(model).where(*criteria).order_by(model.timestamp.desc()).limit(1)
        ).first()

    @staticmethod
    def first_by(
        session: Session,
        model: type[ModelT],
        order_col: ColumnElement,
        *criteria: ColumnElement[bool],
    ) -> ModelT | None:
        """First row ordered by ``order_col`` (asc) matching ``criteria``, or ``None``.

        Used e.g. to pick a user's lowest-id zone (the dashboard default).
        """
        return session.scalars(select(model).where(*criteria).order_by(order_col).limit(1)).first()
