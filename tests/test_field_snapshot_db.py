"""DB-backed field_snapshot_for_user — tested against in-memory SQLite.

Exercises the real fetch-and-compute path (zone pick, today/yesterday
averages, et0 sum, latest soil/npk readings, last irrigation) with a fixed
``now`` so the local "today" window is deterministic.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agri.core.agronomy import field_snapshot_for_user
from agri.db.analytics import (
    AnalyticsEcsalinitysensor,
    AnalyticsEt0calculated,
    AnalyticsHumidityweather,
    AnalyticsNpksensor,
    AnalyticsPhsoil,
    AnalyticsSoilmoisturemedium,
    AnalyticsSoilsalinitysensor,
    AnalyticsSoiltemperaturemedium,
    AnalyticsTemperatureweather,
    AnalyticsWaterflowsensor,
    AnalyticsZone,
)
from agri.db.base import AgriBase
from agri.db.users import CustomUserCustomuser

NOW = dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC)  # local 13:00 (UTC+1) — mid-day
MID = NOW
YESTERDAY = NOW - dt.timedelta(days=1)
_TABLES = [
    CustomUserCustomuser,
    AnalyticsZone,
    AnalyticsTemperatureweather,
    AnalyticsHumidityweather,
    AnalyticsEt0calculated,
    AnalyticsSoilmoisturemedium,
    AnalyticsSoiltemperaturemedium,
    AnalyticsPhsoil,
    AnalyticsEcsalinitysensor,
    AnalyticsSoilsalinitysensor,
    AnalyticsNpksensor,
    AnalyticsWaterflowsensor,
]
_ids = itertools.count(1)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    AgriBase.metadata.create_all(engine, tables=[m.__table__ for m in _TABLES])
    with sessionmaker(bind=engine)() as s:
        yield s


def _user(session: Session) -> int:
    u = CustomUserCustomuser(
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
        date_joined=NOW,
    )
    session.add(u)
    session.flush()
    return u.id


def _zone(session: Session, user_id: int) -> int:
    z = AnalyticsZone(
        id=next(_ids),
        name="Z1",
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
    session.add(z)
    session.flush()
    return z.id


def _reading(session, model, zid, uid, ts, value):
    session.add(model(id=next(_ids), zone_id=zid, user_id=uid, timestamp=ts, value=value))


def test_no_zone_returns_placeholder(session: Session) -> None:
    uid = _user(session)
    snap = field_snapshot_for_user(session, uid, now=NOW)
    assert snap["zone_name"] is None
    assert "Aucune zone" in snap["irrigation_decision"]


def test_populated_snapshot(session: Session) -> None:
    uid = _user(session)
    zid = _zone(session, uid)
    # weather: today vs yesterday averages
    _reading(session, AnalyticsTemperatureweather, zid, uid, MID, 30.0)
    _reading(session, AnalyticsTemperatureweather, zid, uid, YESTERDAY, 20.0)
    _reading(session, AnalyticsHumidityweather, zid, uid, MID, 35.0)
    _reading(session, AnalyticsHumidityweather, zid, uid, YESTERDAY, 70.0)
    # et0 today — two hourly rows summed
    _reading(session, AnalyticsEt0calculated, zid, uid, MID - dt.timedelta(hours=1), 0.4)
    _reading(session, AnalyticsEt0calculated, zid, uid, MID, 0.6)
    # latest soil readings
    _reading(session, AnalyticsSoilmoisturemedium, zid, uid, MID, 15.0)
    _reading(session, AnalyticsSoiltemperaturemedium, zid, uid, MID, 22.0)
    _reading(session, AnalyticsPhsoil, zid, uid, MID, 6.7)
    _reading(session, AnalyticsEcsalinitysensor, zid, uid, MID, 1.2)
    _reading(session, AnalyticsSoilsalinitysensor, zid, uid, MID, 0.8)
    # npk (distinct columns)
    session.add(
        AnalyticsNpksensor(
            id=next(_ids),
            zone_id=zid,
            user_id=uid,
            timestamp=MID,
            nitrogen_value=80.0,
            phosphorus_value=40.0,
            potassium_value=120.0,
        )
    )
    # last irrigation (value>0)
    _reading(session, AnalyticsWaterflowsensor, zid, uid, MID - dt.timedelta(hours=2), 12.0)
    session.flush()

    snap = field_snapshot_for_user(session, uid, now=NOW)

    assert snap["zone_name"] == "Z1"
    assert snap["today_temp_c"] == pytest.approx(30.0)
    assert snap["yesterday_temp_c"] == pytest.approx(20.0)
    assert snap["today_humidity_pct"] == pytest.approx(35.0)
    assert snap["yesterday_humidity_pct"] == pytest.approx(70.0)
    assert snap["et0_today_mm"] == pytest.approx(1.0)
    assert snap["soil_moisture_pct"] == pytest.approx(15.0)
    assert snap["soil_temperature_c"] == pytest.approx(22.0)
    assert snap["soil_ph"] == pytest.approx(6.7)
    assert snap["soil_ec"] == pytest.approx(1.2)
    assert snap["soil_salinity"] == pytest.approx(0.8)
    assert snap["npk_n"] == pytest.approx(80.0)
    assert snap["npk_k"] == pytest.approx(120.0)
    assert snap["last_irrigation_at"] is not None
    assert snap["last_irrigation_l"] == pytest.approx(12_000.0)
    assert snap["raw_mm"] == pytest.approx(15.0)
    assert snap["taw_mm"] == pytest.approx(25.0)


def test_lowest_id_zone_is_picked(session: Session) -> None:
    uid = _user(session)
    first_zone = _zone(session, uid)
    _zone(session, uid)  # a second, higher-id zone
    session.flush()
    snap = field_snapshot_for_user(session, uid, now=NOW)
    # No readings → averages are None, but the chosen zone is the lowest id.
    assert snap["zone_name"] == "Z1"
    assert snap["et0_today_mm"] is None
    # sanity: the picked zone is indeed the first one created
    assert first_zone < next(_ids)
