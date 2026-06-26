"""Multi-day reference-ET₀ forecast (agrilogy-front #18).

Pure, deterministic compute: given N days of daily weather aggregates for a
zone, produce one ET₀ value (mm/day) per day by reusing the existing FAO-56
hourly Penman-Monteith handler (:func:`agri.core.agronomy.compute_zone_et0`).

Daily ET₀ is the sum of the hourly ET₀ over a synthesised diurnal day: each
forecast day's daily means are modulated by a fixed, deterministic diurnal
shape (temperature peaks mid-afternoon, shortwave radiation follows a daylight
sinusoid and is zero at night, humidity runs inverse to temperature). Night
hours contribute ~0 naturally, so no daylight-hours fudge factor is needed.

This module has NO I/O and NO data source of its own — the *weather* comes
from a provider (mock by default, real provider behind an env flag) wired in
the agri-api adapter. Keeping the math here means the same forecast compute is
reusable by any future consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import pi, sin

from agri.core.agronomy import Et0Inputs, compute_zone_et0


@dataclass
class DailyWeatherForecast:
    """One forecast day's weather, as daily aggregates in sensor units.

    ``rs_wm2`` is the midday PEAK shortwave irradiance (W/m²) — the diurnal
    model scales it down across the day; the other fields are daily means.
    """

    day: date
    temp_c: float
    rh_pct: float
    wind_ms: float
    rs_wm2: float
    pressure_hpa: float


# Diurnal shape constants (deterministic; tuned for a temperate/Mediterranean
# day, which suits the deployment region). Amplitudes are intentionally modest
# so the synthesised day stays physically plausible.
_TEMP_AMPLITUDE_C = 5.0
_TEMP_PEAK_HOUR = 15.0
_RH_AMPLITUDE_PCT = 18.0
_SUNRISE_HOUR = 6.0
_SUNSET_HOUR = 18.0


def _temp_at(hour: float, daily_mean_c: float) -> float:
    """Sinusoidal temperature peaking at ``_TEMP_PEAK_HOUR``."""
    return daily_mean_c + _TEMP_AMPLITUDE_C * sin(2.0 * pi * (hour - _TEMP_PEAK_HOUR + 6.0) / 24.0)


def _rh_at(hour: float, daily_mean_pct: float) -> float:
    """Humidity inverse to temperature, clamped to [1, 100]."""
    rh = daily_mean_pct - _RH_AMPLITUDE_PCT * sin(2.0 * pi * (hour - _TEMP_PEAK_HOUR + 6.0) / 24.0)
    return max(1.0, min(100.0, rh))


def _rs_at(hour: float, peak_wm2: float) -> float:
    """Shortwave irradiance: zero at night, half-sine over daylight."""
    if hour <= _SUNRISE_HOUR or hour >= _SUNSET_HOUR:
        return 0.0
    daylight = _SUNSET_HOUR - _SUNRISE_HOUR
    return max(0.0, peak_wm2) * sin(pi * (hour - _SUNRISE_HOUR) / daylight)


def daily_et0_mm(
    forecast: DailyWeatherForecast,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    elevation_m: float = 0.0,
) -> float:
    """Sum the hourly FAO-56 ET₀ over a synthesised diurnal day → mm/day.

    Deterministic for a given ``forecast`` + location. Returns ``0.0`` only if
    every hour is uncomputable (which shouldn't happen for valid inputs)."""
    total_mm = 0.0
    for hour in range(24):
        ts = datetime(
            forecast.day.year,
            forecast.day.month,
            forecast.day.day,
            hour,
            30,  # mid-hour, matching the live hourly window's midpoint
            tzinfo=UTC,
        )
        hourly = compute_zone_et0(
            Et0Inputs(
                zone_id=0,
                user_id=0,
                timestamp=ts,
                temp_c=_temp_at(hour + 0.5, forecast.temp_c),
                rh_pct=_rh_at(hour + 0.5, forecast.rh_pct),
                wind_ms=forecast.wind_ms,
                rs_wm2=_rs_at(hour + 0.5, forecast.rs_wm2),
                pressure_hpa=forecast.pressure_hpa,
                latitude=latitude,
                longitude=longitude,
                elevation_m=elevation_m,
            )
        )
        if hourly is not None:
            total_mm += hourly.et0_mm_per_h
    return total_mm


def et0_forecast(
    days: list[DailyWeatherForecast],
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    elevation_m: float = 0.0,
) -> list[dict[str, float | str]]:
    """Map N forecast days → ``[{date, et0_mm}, ...]`` (et0_mm rounded to 2dp)."""
    out: list[dict[str, float | str]] = []
    for d in days:
        et0 = daily_et0_mm(d, latitude=latitude, longitude=longitude, elevation_m=elevation_m)
        out.append({"date": d.day.isoformat(), "et0_mm": round(et0, 2)})
    return out
