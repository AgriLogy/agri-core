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

from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from agri.db.base import AgriBase
from agri.db.devices import AnalyticsDevice

ModelT = TypeVar("ModelT", bound=AgriBase)


def _owner_user(model: type[AgriBase]) -> ColumnElement:
    """Effective owning user of a reading: its device's ``user_id`` when the row
    is device-sourced (``device_id`` set), else the row's own ``user_id``."""
    return func.coalesce(AnalyticsDevice.user_id, model.user_id)


def _owner_zone(model: type[AgriBase]) -> ColumnElement:
    """Effective owning zone of a reading: its device's ``zone_id`` when the row
    is device-sourced, else the row's own ``zone_id``. Resolving ownership via
    the device (JOIN to ``analytics_device`` on ``device_id``) is what makes a
    device transfer a one-row update that instantly moves the device's history —
    no reading rewrite."""
    return func.coalesce(AnalyticsDevice.zone_id, model.zone_id)


def _with_device_join(stmt, model: type[AgriBase]):
    """LEFT JOIN ``analytics_device`` on ``model.device_id`` so ``_owner_user`` /
    ``_owner_zone`` can resolve. LEFT so non-device rows (``device_id`` NULL)
    survive and fall back to their own user/zone."""
    return stmt.select_from(model).outerjoin(AnalyticsDevice, model.device_id == AnalyticsDevice.id)


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
            _with_device_join(select(func.avg(model.value)), model).where(
                _owner_zone(model) == zone_id,
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
            _with_device_join(select(func.sum(model.value)), model).where(
                _owner_zone(model) == zone_id,
                model.timestamp >= start,
                model.timestamp < end,
            )
        )

    @staticmethod
    def hourly_averages(
        session: Session,
        model: type[ModelT],
        *,
        user_id: int,
        zone_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        value_columns: Sequence[str] = ("value",),
    ) -> list[dict[str, Any]]:
        """One row per clock hour for ``user_id``'s readings, averaging each
        of ``value_columns`` within the hour.

        Buckets rows with ``date_trunc('hour', timestamp)`` (Postgres) and
        returns dicts ``{"hour": datetime, "last_id": int, <col>: float | None,
        ...}`` ordered by hour ascending — the canonical aggregation behind
        the django-ninja sensors router's one-value-per-hour-per-captor
        response. NPK passes its three ``*_value`` columns; every other
        sensor takes the default ``("value",)``.

        ``zone_id`` / ``start`` / ``end`` are optional filters (``end`` is
        exclusive). ``last_id`` is the max row id in the bucket, so the API
        layer keeps a unique, patchable id per aggregated row. Empty list
        when no rows match.
        """
        bucket = func.date_trunc("hour", model.timestamp).label("hour")
        selected = [bucket, func.max(model.id).label("last_id")]
        selected += [func.avg(getattr(model, c)).label(c) for c in value_columns]
        # Ownership is resolved via the device (JOIN to analytics_device) so a
        # device-sourced reading follows its device on transfer; non-device rows
        # fall back to their own user_id/zone_id.
        stmt = _with_device_join(select(*selected), model).where(_owner_user(model) == user_id)
        if zone_id is not None:
            stmt = stmt.where(_owner_zone(model) == zone_id)
        if start is not None:
            stmt = stmt.where(model.timestamp >= start)
        if end is not None:
            stmt = stmt.where(model.timestamp < end)
        stmt = stmt.group_by(bucket).order_by(bucket)
        return [dict(row._mapping) for row in session.execute(stmt)]

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
    def latest_reading(
        session: Session,
        model: type[ModelT],
        *,
        user_id: int,
        zone_id: int | None = None,
    ) -> ModelT | None:
        """Most recent reading owned by ``zone_id`` (or ``user_id`` when no zone),
        resolving ownership via the device JOIN so device-sourced rows follow
        their device on transfer. Mirrors the alert engine's zone-or-user scope.
        """
        stmt = _with_device_join(select(model), model)
        if zone_id:
            stmt = stmt.where(_owner_zone(model) == zone_id)
        else:
            stmt = stmt.where(_owner_user(model) == user_id)
        return session.scalars(stmt.order_by(model.timestamp.desc()).limit(1)).first()

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
