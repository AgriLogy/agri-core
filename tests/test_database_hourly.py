"""Behavioural tests for ``AgriMainDBClient.hourly_averages``.

``date_trunc('hour', …)`` is Postgres-only, so we register a SQLite UDF that
mimics it — letting the real averaging / grouping / zone-scoping run against
in-memory SQLite (the same harness the other ``*_db`` tests use).
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agri.core.database import AgriMainDBClient
from agri.db.analytics import AnalyticsTemperatureweather
from agri.db.base import AgriBase

_ids = itertools.count(1)


def _trunc_hour(unit, value):  # noqa: ANN001 - SQLite UDF signature
    """Mimic ``date_trunc('hour', ts)`` over SQLite's stored ISO string,
    e.g. ``"2026-06-09 10:05:00.000000+00:00"`` → ``"2026-06-09 10:00:00"``.
    """
    return value[:13] + ":00:00" if value else value


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_udf(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.create_function("date_trunc", 2, _trunc_hour)

    AgriBase.metadata.create_all(
        engine, tables=[AnalyticsTemperatureweather.__table__]
    )
    with sessionmaker(bind=engine)() as s:
        yield s


def _temp(session, ts, value, *, zone_id=1, uid=1):
    session.add(
        AnalyticsTemperatureweather(
            id=next(_ids), zone_id=zone_id, user_id=uid, timestamp=ts, value=value
        )
    )


def _at(hour, minute):
    return dt.datetime(2026, 6, 9, hour, minute, tzinfo=dt.UTC)


DAY_START = dt.datetime(2026, 6, 9, tzinfo=dt.UTC)
DAY_END = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)


def test_hourly_averages_one_row_per_hour(session: Session) -> None:
    # 10:00 → avg(10, 20) = 15 ; 11:00 → avg(30) = 30
    _temp(session, _at(10, 5), 10.0)
    _temp(session, _at(10, 55), 20.0)
    _temp(session, _at(11, 30), 30.0)
    session.flush()

    rows = AgriMainDBClient.hourly_averages(
        session,
        AnalyticsTemperatureweather,
        zone_id=1,
        start=DAY_START,
        end=DAY_END,
    )
    assert [v for _, v in rows] == [15.0, 30.0]
    assert rows[0][0].startswith("2026-06-09 10")  # buckets ordered ascending
    assert rows[1][0].startswith("2026-06-09 11")


def test_hourly_averages_scopes_by_zone(session: Session) -> None:
    _temp(session, _at(10, 5), 10.0, zone_id=1)
    _temp(session, _at(10, 6), 99.0, zone_id=2)  # different zone — excluded
    session.flush()

    rows = AgriMainDBClient.hourly_averages(
        session,
        AnalyticsTemperatureweather,
        zone_id=1,
        start=DAY_START,
        end=DAY_END,
    )
    assert [v for _, v in rows] == [10.0]


def test_hourly_averages_honours_time_window(session: Session) -> None:
    _temp(session, _at(10, 5), 10.0)
    _temp(session, _at(23, 5), 99.0)  # outside the [start, end) window below
    session.flush()

    rows = AgriMainDBClient.hourly_averages(
        session,
        AnalyticsTemperatureweather,
        zone_id=1,
        start=_at(9, 0),
        end=_at(12, 0),
    )
    assert [v for _, v in rows] == [10.0]


def test_hourly_averages_empty_when_no_rows(session: Session) -> None:
    rows = AgriMainDBClient.hourly_averages(
        session,
        AnalyticsTemperatureweather,
        zone_id=1,
        start=DAY_START,
        end=DAY_END,
    )
    assert rows == []
