"""Behavioural tests for ``AgriMainDBClient.hourly_averages``.

``date_trunc('hour', …)`` is Postgres-only, so we register a SQLite UDF that
mimics it — letting the real averaging / grouping / scoping run against
in-memory SQLite (the same harness the other ``*_db`` tests use).
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agri.core.database import AgriMainDBClient
from agri.db.analytics import AnalyticsNpksensor, AnalyticsTemperatureweather
from agri.db.base import AgriBase

_ids = itertools.count(1)


def _trunc_hour(unit, value):  # noqa: ANN001 - SQLite UDF signature
    """Mimic ``date_trunc('hour', ts)`` over SQLite's stored ISO string,
    e.g. ``"2026-06-09 10:05:00.000000+00:00"`` → ``"2026-06-09 10:00:00"``.
    """
    return value[:13] + ":00:00" if value else value


def _make_session(*tables) -> Session:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_udf(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.create_function("date_trunc", 2, _trunc_hour)

    AgriBase.metadata.create_all(engine, tables=[t.__table__ for t in tables])
    return sessionmaker(bind=engine)()


@pytest.fixture
def session() -> Session:
    with _make_session(AnalyticsTemperatureweather) as s:
        yield s


@pytest.fixture
def npk_session() -> Session:
    with _make_session(AnalyticsNpksensor) as s:
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
        session, AnalyticsTemperatureweather, user_id=1, start=DAY_START, end=DAY_END
    )
    assert [r["value"] for r in rows] == [15.0, 30.0]
    assert str(rows[0]["hour"]).startswith("2026-06-09 10")  # buckets ascending
    assert str(rows[1]["hour"]).startswith("2026-06-09 11")


def test_hourly_averages_returns_max_id_per_bucket(session: Session) -> None:
    """``last_id`` is the highest row id in the hour, so the API keeps a
    unique, patchable id per aggregated row."""
    _temp(session, _at(10, 5), 10.0)
    last = next(_ids)
    session.add(
        AnalyticsTemperatureweather(
            id=last, zone_id=1, user_id=1, timestamp=_at(10, 55), value=20.0
        )
    )
    session.flush()

    rows = AgriMainDBClient.hourly_averages(session, AnalyticsTemperatureweather, user_id=1)
    assert len(rows) == 1
    assert rows[0]["last_id"] == last


def test_hourly_averages_scopes_by_user_and_zone(session: Session) -> None:
    _temp(session, _at(10, 5), 10.0, uid=1, zone_id=1)
    _temp(session, _at(10, 6), 88.0, uid=2, zone_id=1)  # other user — excluded
    _temp(session, _at(10, 7), 99.0, uid=1, zone_id=2)  # other zone — excluded
    session.flush()

    rows = AgriMainDBClient.hourly_averages(
        session, AnalyticsTemperatureweather, user_id=1, zone_id=1
    )
    assert [r["value"] for r in rows] == [10.0]


def test_hourly_averages_honours_time_window(session: Session) -> None:
    _temp(session, _at(10, 5), 10.0)
    _temp(session, _at(23, 5), 99.0)  # outside the window below
    session.flush()

    rows = AgriMainDBClient.hourly_averages(
        session, AnalyticsTemperatureweather, user_id=1, start=_at(9, 0), end=_at(12, 0)
    )
    assert [r["value"] for r in rows] == [10.0]


def test_hourly_averages_multi_column_for_npk(npk_session: Session) -> None:
    for n, p, k in [(10.0, 100.0, 1.0), (20.0, 200.0, 3.0)]:
        npk_session.add(
            AnalyticsNpksensor(
                id=next(_ids),
                zone_id=1,
                user_id=1,
                timestamp=_at(10, 5),
                nitrogen_value=n,
                phosphorus_value=p,
                potassium_value=k,
            )
        )
    npk_session.flush()

    rows = AgriMainDBClient.hourly_averages(
        npk_session,
        AnalyticsNpksensor,
        user_id=1,
        value_columns=("nitrogen_value", "phosphorus_value", "potassium_value"),
    )
    assert len(rows) == 1
    assert rows[0]["nitrogen_value"] == 15.0
    assert rows[0]["phosphorus_value"] == 150.0
    assert rows[0]["potassium_value"] == 2.0
    assert "value" not in rows[0]


def test_hourly_averages_empty_when_no_rows(session: Session) -> None:
    rows = AgriMainDBClient.hourly_averages(session, AnalyticsTemperatureweather, user_id=1)
    assert rows == []
