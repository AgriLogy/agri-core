"""Sheltered / indoor weather-station detection."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from agri.core.station_exposure import (
    SENSOR_NOT_CHECKED,
    SENSOR_OK,
    SENSOR_SHELTERED,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    STATUS_SHELTERED,
    STATUS_SUSPECT,
    ExposureHour,
    assess_station_exposure,
)

DAY = datetime(2026, 11, 24, tzinfo=UTC)


def _outdoor(h: int) -> tuple[float, float, float]:
    """A late-November day in Morocco: (temp °C, wind 10 m m/s, shortwave W/m²)."""
    temp = 21.0 - 6.0 * math.cos(2 * math.pi * (h - 3) / 24)  # 15 °C at 03:00, 27 °C at 15:00
    rs = max(0.0, 600.0 * math.sin(math.pi * (h - 7) / 11)) if 7 < h < 18 else 0.0
    wind = 3.0 + math.sin(h / 3)
    return temp, wind, rs


def _day(station) -> list[ExposureHour]:
    out = []
    for h in range(24):
        t, w, rs = _outdoor(h)
        st = station(h, t, w, rs)
        out.append(
            ExposureHour(
                hour=DAY + timedelta(hours=h),
                station_temp_c=st[0],
                station_wind_ms=st[1],
                station_rs_wm2=st[2],
                ref_temp_c=t,
                ref_wind_ms=w,
                ref_rs_wm2=rs,
            )
        )
    return out


def test_a_well_installed_station_is_ok():
    # Tracks the outside closely (a little sensor noise, 2 m wind < 10 m wind).
    report = assess_station_exposure(
        _day(lambda h, t, w, rs: (t + 0.4 * math.sin(h), 0.75 * w, rs * 0.95))
    )
    assert report.status == STATUS_OK and report.signals == []
    assert report.hours_compared == 24
    assert report.sensors == {"solar": SENSOR_OK, "wind": SENSOR_OK, "temperature": SENSOR_OK}


# Router02, 2026-11-23 14:00 → 11-24 14:00 UTC: (hour, station °C, Open-Meteo °C,
# Open-Meteo wind 10 m m/s, Open-Meteo shortwave W/m²). The station sat in a dark,
# closed space: pyranometer 0 W/m² and anemometer 0 m/s all day.
ROUTER02 = [
    (14, 26.1, 27.1, 3.64, 568.0), (15, 25.2, 26.8, 3.68, 456.0), (16, 24.7, 26.3, 3.16, 302.0),
    (17, 24.5, 25.1, 2.1, 120.0), (18, 24.1, 22.3, 1.17, 5.0), (19, 23.7, 20.6, 0.95, 0.0),
    (20, 23.5, 20.3, 0.85, 0.0), (21, 23.3, 20.3, 0.81, 0.0), (22, 23.2, 20.1, 0.94, 0.0),
    (23, 23.1, 19.7, 1.12, 0.0), (0, 22.9, 19.0, 1.1, 0.0), (1, 22.7, 18.6, 1.2, 0.0),
    (2, 22.6, 17.3, 1.46, 0.0), (3, 22.6, 15.8, 1.61, 0.0), (4, 22.5, 15.4, 1.66, 0.0),
    (5, 22.4, 15.5, 1.88, 0.0), (6, 22.2, 15.2, 2.06, 0.0), (7, 22.1, 15.0, 2.19, 0.0),
    (8, 22.1, 16.1, 2.25, 41.0), (9, 22.7, 19.3, 2.16, 184.0), (10, 23.8, 21.8, 2.91, 348.0),
    (11, 25.1, 23.8, 3.47, 501.0), (12, 25.8, 24.8, 3.28, 519.0), (13, 25.7, 25.1, 3.85, 494.0),
]  # fmt: skip


def test_router02_enclosed_station_is_sheltered():
    hours = [
        ExposureHour(
            hour=DAY + timedelta(hours=i),
            station_temp_c=st,
            station_wind_ms=0.0,
            station_rs_wm2=0.0,
            ref_temp_c=rt,
            ref_wind_ms=rw,
            ref_rs_wm2=rr,
        )
        for i, (_, st, rt, rw, rr) in enumerate(ROUTER02)
    ]
    report = assess_station_exposure(hours)
    assert report.status == STATUS_SHELTERED
    assert set(report.codes) == {"no_sun", "no_wind", "damped_temperature", "warm_nights"}
    detail = {s.code: s.detail for s in report.signals}
    assert detail["damped_temperature"]["station_range_c"] == 4.0
    assert detail["warm_nights"]["mean_bias_c"] >= 4.0
    assert report.sensors == {
        "solar": SENSOR_SHELTERED,
        "wind": SENSOR_SHELTERED,
        "temperature": SENSOR_SHELTERED,
    }
    d = report.as_dict()
    assert d["status"] == "sheltered" and len(d["signals"]) == 4
    assert d["sensors"]["solar"] == "sheltered" and len(d["checked"]) == 4


def test_only_a_shaded_pyranometer_is_suspect():
    report = assess_station_exposure(_day(lambda h, t, w, rs: (t, 0.75 * w, 5.0)))
    assert report.status == STATUS_SUSPECT and report.codes == ["no_sun"]
    # Only the pyranometer is in the shade: wind and temperature read outdoors.
    assert report.sensors == {
        "solar": SENSOR_SHELTERED,
        "wind": SENSOR_OK,
        "temperature": SENSOR_OK,
    }


def test_a_calm_day_is_not_a_sheltered_anemometer():
    # Station 0 m/s, but the reference is calm too: no signal.
    hours = [
        ExposureHour(
            hour=h.hour,
            station_temp_c=h.ref_temp_c,
            station_wind_ms=0.0,
            station_rs_wm2=h.ref_rs_wm2,
            ref_temp_c=h.ref_temp_c,
            ref_wind_ms=0.4,
            ref_rs_wm2=h.ref_rs_wm2,
        )
        for h in _day(lambda h, t, w, rs: (t, 0.0, rs))
    ]
    report = assess_station_exposure(hours)
    assert report.status == STATUS_OK
    assert report.sensors["wind"] == SENSOR_NOT_CHECKED  # calm day: can't tell


def test_overcast_day_does_not_trip_no_sun():
    # Reference never reaches daylight threshold → the sun signal isn't judged.
    hours = [
        ExposureHour(
            hour=h.hour,
            station_temp_c=h.station_temp_c,
            station_wind_ms=h.station_wind_ms,
            station_rs_wm2=0.0,
            ref_temp_c=h.ref_temp_c,
            ref_wind_ms=h.ref_wind_ms,
            ref_rs_wm2=min(h.ref_rs_wm2 or 0.0, 150.0),
        )
        for h in _day(lambda h, t, w, rs: (t, 0.75 * w, rs))
    ]
    report = assess_station_exposure(hours)
    assert "no_sun" not in report.codes
    assert report.sensors["solar"] == SENSOR_NOT_CHECKED


def test_missing_sensors_are_skipped_not_guessed():
    # Temperature-only station, tracking outside: judged ok on what it has.
    report = assess_station_exposure(_day(lambda h, t, w, rs: (t, None, None)))
    assert report.status == STATUS_OK
    assert report.sensors == {
        "solar": SENSOR_NOT_CHECKED,
        "wind": SENSOR_NOT_CHECKED,
        "temperature": SENSOR_OK,
    }


def test_too_few_hours_is_insufficient():
    report = assess_station_exposure(_day(lambda h, t, w, rs: (t, 0.75 * w, rs))[:6])
    assert report.status == STATUS_INSUFFICIENT and report.hours_compared == 6
