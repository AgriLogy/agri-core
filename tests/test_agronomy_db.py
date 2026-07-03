"""DB-backed ET₀ entry point — tested against in-memory SQLite.

The agri.db reading/zone/user models use only portable column types
(BigInteger / DateTime / Double / String / Boolean), so we create just
the ET₀-relevant subset of tables on SQLite and exercise the real
fetch-and-compute path through ``compute_et0_for_zone`` and
``AgriMainDBClient.average_value`` — no Postgres required.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agri.core.agronomy import Et0Inputs, compute_et0_for_zone, compute_zone_et0
from agri.core.database import AgriMainDBClient
from agri.db.analytics import (
    AnalyticsHumidityweather,
    AnalyticsPressureweather,
    AnalyticsSolarradiation,
    AnalyticsTemperatureweather,
    AnalyticsWindspeed,
    AnalyticsZone,
)
from agri.db.base import AgriBase
from agri.db.users import CustomUserCustomuser

END = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)  # a fixed, hour-aligned slot
MID = END - dt.timedelta(minutes=30)  # inside [END-1h, END)
WEATHER_MODELS = [
    AnalyticsTemperatureweather,
    AnalyticsHumidityweather,
    AnalyticsWindspeed,
    AnalyticsSolarradiation,
    AnalyticsPressureweather,
]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    tables = [m.__table__ for m in (CustomUserCustomuser, AnalyticsZone, *WEATHER_MODELS)]
    AgriBase.metadata.create_all(engine, tables=tables)
    with sessionmaker(bind=engine)() as s:
        yield s


# Identity PKs don't autoincrement on SQLite (BigInteger isn't the rowid
# alias), so the fixtures assign explicit ids.
_ids = itertools.count(1)


def _make_user(session: Session, *, lat: float | None = 31.6, lon: float | None = -8.0) -> int:
    user = CustomUserCustomuser(
        id=next(_ids),
        password="x",
        is_superuser=False,
        username="u",
        firstname="F",
        lastname="L",
        email="u@example.com",
        payement_status="ok",
        is_active=True,
        is_staff=False,
        is_technician=False,
        notify_every=24,
        date_joined=END,
        latitude=lat,
        longitude=lon,
    )
    session.add(user)
    session.flush()
    return user.id


def _make_zone(session: Session, user_id: int) -> int:
    zone = AnalyticsZone(
        id=next(_ids),
        name="Z",
        space=1000.0,
        critical_moisture_threshold=20.0,
        user_id=user_id,
        irrigation_water_quantity=500.0,
        pomp_flow_rate=2.0,
        soil_param_FC=30.0,
        soil_param_RAW=15.0,
        soil_param_TAW=25.0,
        soil_param_WP=10.0,
    )
    session.add(zone)
    session.flush()
    return zone.id


def _add_reading(session: Session, model: type, zone_id: int, user_id: int, ts, value) -> None:
    session.add(model(id=next(_ids), zone_id=zone_id, user_id=user_id, timestamp=ts, value=value))


# --- average_value -----------------------------------------------------------


def test_average_value_window_and_empty(session: Session) -> None:
    uid = _make_user(session)
    zid = _make_zone(session, uid)
    M = AnalyticsTemperatureweather
    _add_reading(session, M, zid, uid, MID, 20.0)  # in window
    _add_reading(session, M, zid, uid, MID - dt.timedelta(minutes=10), 30.0)  # in window
    _add_reading(session, M, zid, uid, END, 99.0)  # == end → excluded (half-open)
    _add_reading(session, M, zid, uid, END - dt.timedelta(hours=2), 99.0)  # before window
    session.flush()

    start = END - dt.timedelta(hours=1)
    assert AgriMainDBClient.average_value(session, M, zone_id=zid, start=start, end=END) == 25.0
    # No rows for a different zone → None (mirrors Django Avg on empty).
    assert AgriMainDBClient.average_value(session, M, zone_id=999, start=start, end=END) is None


# --- compute_et0_for_zone ----------------------------------------------------


def _seed_full_hour(session: Session, zid: int, uid: int) -> dict[type, float]:
    values = {
        AnalyticsTemperatureweather: 25.0,
        AnalyticsHumidityweather: 50.0,
        AnalyticsWindspeed: 2.0,
        AnalyticsSolarradiation: 600.0,
        AnalyticsPressureweather: 1013.0,
    }
    for model, val in values.items():
        _add_reading(session, model, zid, uid, MID, val)
    session.flush()
    return values


def test_compute_et0_for_zone_matches_pure_handler(session: Session) -> None:
    uid = _make_user(session, lat=31.6, lon=-8.0)
    zid = _make_zone(session, uid)
    vals = _seed_full_hour(session, zid, uid)

    result = compute_et0_for_zone(session, zid, end=END)

    expected = compute_zone_et0(
        Et0Inputs(
            zone_id=zid,
            user_id=uid,
            timestamp=END,
            temp_c=vals[AnalyticsTemperatureweather],
            rh_pct=vals[AnalyticsHumidityweather],
            wind_ms=vals[AnalyticsWindspeed],
            rs_wm2=vals[AnalyticsSolarradiation],
            pressure_hpa=vals[AnalyticsPressureweather],
            latitude=31.6,
            longitude=-8.0,
        )
    )
    assert result is not None and expected is not None
    assert result == expected
    assert result.zone_id == zid and result.user_id == uid and result.timestamp == END


def test_compute_et0_for_zone_none_when_a_sensor_missing(session: Session) -> None:
    uid = _make_user(session)
    zid = _make_zone(session, uid)
    _seed_full_hour(session, zid, uid)
    # Drop the pressure readings → a required input is missing for the slot.
    session.query(AnalyticsPressureweather).delete()
    session.flush()
    assert compute_et0_for_zone(session, zid, end=END) is None


def test_compute_et0_for_zone_none_when_zone_unknown(session: Session) -> None:
    assert compute_et0_for_zone(session, 12345, end=END) is None
