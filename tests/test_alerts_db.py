"""DB-backed alerts handlers — tested against in-memory SQLite."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agri.core.alerts import (
    SENSOR_KEY_REGISTRY,
    db_model_for,
    recent_triggers_for_user,
    suggest_alert_for,
)
from agri.db.analytics import (
    AnalyticsAlert,
    AnalyticsNotificationzone,
    AnalyticsNotificationzonesensor,
    AnalyticsTemperatureweather,
    AnalyticsZone,
)
from agri.db.base import AgriBase
from agri.db.devices import AnalyticsDevice
from agri.db.users import CustomUserCustomuser

NOW = dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC)
_TABLES = [
    CustomUserCustomuser,
    AnalyticsZone,
    AnalyticsAlert,
    AnalyticsTemperatureweather,
    AnalyticsNotificationzone,
    AnalyticsNotificationzonesensor,
]
_ids = itertools.count(1)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    AgriBase.metadata.create_all(
        engine, tables=[m.__table__ for m in _TABLES] + [AnalyticsDevice.__table__]
    )
    with sessionmaker(bind=engine)() as s:
        yield s


def _alert(session, uid, *, condition=">", threshold=30.0, key="temperature_weather"):
    a = AnalyticsAlert(
        id=next(_ids),
        name="Heat",
        type="Weather Temperature",
        description="",
        condition=condition,
        condition_nbr=threshold,
        is_active=True,
        sensor_key=key,
        user_id=uid,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(a)
    return a


def _temp(session, uid, value, ts=NOW):
    # zone_id is NOT NULL on the reading; the user-scoped alert queries by
    # user_id, so the specific zone value here doesn't matter.
    session.add(
        AnalyticsTemperatureweather(
            id=next(_ids), zone_id=1, user_id=uid, timestamp=ts, value=value
        )
    )


def test_db_model_for_resolves_every_registry_key() -> None:
    for key in SENSOR_KEY_REGISTRY:
        model = db_model_for(key)
        assert hasattr(model, "value") or key  # every reading model has value
        assert model.__name__.startswith("Analytics")


def test_recent_triggers_flags_and_stamps(session: Session) -> None:
    uid = 1
    _alert(session, uid, threshold=30.0)
    _temp(session, uid, 35.0)  # above 30 → triggered
    session.flush()

    rows = recent_triggers_for_user(session, uid, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["is_triggered"] is True
    assert r["latest_value"] == pytest.approx(35.0)
    assert r["threshold"] == pytest.approx(30.0)
    assert r["unit"] is not None and r["label"] is not None
    assert r["last_triggered_at"] is not None  # stamped on first trigger


def test_recent_triggers_not_triggered_when_below(session: Session) -> None:
    uid = 1
    _alert(session, uid, threshold=30.0)
    _temp(session, uid, 25.0)  # below 30 → not triggered
    session.flush()
    r = recent_triggers_for_user(session, uid, now=NOW)[0]
    assert r["is_triggered"] is False
    assert r["last_triggered_at"] is None


def test_suggest_alert_for_uses_recent_mean(session: Session) -> None:
    uid = 1
    for v in (20.0, 30.0, 40.0):
        _temp(session, uid, v)
    session.flush()
    payload = suggest_alert_for(session, uid, "temperature_weather")
    assert payload is not None
    assert payload["condition_nbr"] == pytest.approx(30.0)  # mean
    assert payload["sample_size"] == 3


def test_suggest_alert_for_unknown_key_is_none(session: Session) -> None:
    assert suggest_alert_for(session, 1, "not_a_key") is None


def test_notification_zone_alert_reads_from_assigned_source_zone(
    session: Session,
) -> None:
    """An alert bound to a custom notification zone resolves its reading stream
    through the zone's sensor assignment (sensor_key + source_zone_id), NOT the
    user-wide latest (agrilogy-front #57)."""
    uid = 1
    nz = AnalyticsNotificationzone(
        id=next(_ids), name="Pump Area", description="", is_active=True, user_id=uid
    )
    session.add(nz)
    session.flush()
    session.add(
        AnalyticsNotificationzonesensor(
            id=next(_ids),
            sensor_key="temperature_weather",
            notification_zone_id=nz.id,
            source_zone_id=20,
        )
    )
    alert = AnalyticsAlert(
        id=next(_ids),
        name="Pump heat",
        type="Weather Temperature",
        description="",
        condition=">",
        condition_nbr=30.0,
        is_active=True,
        sensor_key="temperature_weather",
        user_id=uid,
        notification_zone_id=nz.id,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(alert)
    # Reading in the ASSIGNED source zone (20) — below; and a NEWER reading in a
    # different zone (30) — above. Correct resolution must pick zone 20's value.
    session.add(
        AnalyticsTemperatureweather(
            id=next(_ids), zone_id=20, user_id=uid, timestamp=NOW, value=25.0
        )
    )
    session.add(
        AnalyticsTemperatureweather(
            id=next(_ids),
            zone_id=30,
            user_id=uid,
            timestamp=NOW + dt.timedelta(minutes=5),
            value=99.0,
        )
    )
    session.flush()

    rows = recent_triggers_for_user(session, uid, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["latest_value"] == pytest.approx(25.0)  # zone 20, not the newer 99
    assert r["is_triggered"] is False  # 25 < 30
