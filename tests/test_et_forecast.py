"""Tests for the multi-day ET₀ forecast compute (agrilogy-front #18)."""

from __future__ import annotations

from datetime import date

from agri.core.et_forecast import (
    DailyWeatherForecast,
    daily_et0_mm,
    et0_forecast,
)

# A clear, warm summer-ish day in the deployment region.
_SUNNY = DailyWeatherForecast(
    day=date(2026, 7, 1),
    temp_c=28.0,
    rh_pct=45.0,
    wind_ms=2.5,
    rs_wm2=800.0,
    pressure_hpa=1013.0,
)
_LAT, _LON = 33.57, -7.59  # Casablanca-ish


def test_deterministic():
    a = daily_et0_mm(_SUNNY, latitude=_LAT, longitude=_LON)
    b = daily_et0_mm(_SUNNY, latitude=_LAT, longitude=_LON)
    assert a == b


def test_sunny_day_in_physical_range():
    et0 = daily_et0_mm(_SUNNY, latitude=_LAT, longitude=_LON, elevation_m=50.0)
    # A warm sunny day's reference ET₀ is realistically ~3–12 mm/day.
    assert 3.0 <= et0 <= 12.0


def test_hotter_brighter_day_has_more_et0():
    cool = DailyWeatherForecast(
        day=date(2026, 1, 15),
        temp_c=12.0,
        rh_pct=80.0,
        wind_ms=1.0,
        rs_wm2=300.0,
        pressure_hpa=1013.0,
    )
    et0_hot = daily_et0_mm(_SUNNY, latitude=_LAT, longitude=_LON)
    et0_cool = daily_et0_mm(cool, latitude=_LAT, longitude=_LON)
    assert et0_hot > et0_cool


def test_no_sun_gives_low_et0():
    overcast = DailyWeatherForecast(
        day=date(2026, 7, 1),
        temp_c=28.0,
        rh_pct=45.0,
        wind_ms=2.5,
        rs_wm2=0.0,
        pressure_hpa=1013.0,
    )
    assert daily_et0_mm(overcast, latitude=_LAT, longitude=_LON) < daily_et0_mm(
        _SUNNY, latitude=_LAT, longitude=_LON
    )


def test_forecast_shape_and_rounding():
    days = [
        DailyWeatherForecast(
            day=date(2026, 7, d),
            temp_c=26.0 + d,
            rh_pct=50.0,
            wind_ms=2.0,
            rs_wm2=750.0,
            pressure_hpa=1013.0,
        )
        for d in range(1, 8)
    ]
    out = et0_forecast(days, latitude=_LAT, longitude=_LON)
    assert len(out) == 7
    assert [row["date"] for row in out] == [f"2026-07-0{d}" for d in range(1, 8)]
    for row in out:
        assert isinstance(row["et0_mm"], float)
        assert row["et0_mm"] >= 0.0
        # rounded to 2 dp
        assert round(row["et0_mm"], 2) == row["et0_mm"]


def test_works_without_latlon_fallback():
    # No lat/lon → Ra falls back to the 0.75 heuristic, still deterministic.
    a = daily_et0_mm(_SUNNY)
    b = daily_et0_mm(_SUNNY)
    assert a == b and a > 0.0
