"""DB-backed compose_notification_for_user — in-memory SQLite."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agri.core.notifications import compose_notification_for_user
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

NOW = dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC)
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


def _user(session: Session, firstname: str = "Zak") -> int:
    u = CustomUserCustomuser(
        id=next(_ids),
        password="x",
        is_superuser=False,
        username="zak",
        firstname=firstname,
        lastname="L",
        email="z@example.com",
        payement_status="ok",
        is_active=True,
        is_staff=False,
        notify_every=24,
        date_joined=NOW,
    )
    session.add(u)
    session.flush()
    return u.id


def _zone(session: Session, user_id: int) -> None:
    session.add(
        AnalyticsZone(
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
    )
    session.flush()


def test_composes_email_for_user(session: Session) -> None:
    uid = _user(session, firstname="Zak")
    _zone(session, uid)
    body = compose_notification_for_user(session, uid, now=NOW)
    assert body is not None
    assert "Bonjour Zak," in body
    assert "Z1" in body  # zone name
    assert "rapport quotidien" in body


def test_unknown_user_returns_none(session: Session) -> None:
    assert compose_notification_for_user(session, 12345, now=NOW) is None
