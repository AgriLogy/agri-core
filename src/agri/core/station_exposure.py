"""Is a weather station measuring the field, or sitting sheltered / indoors?

A station installed in a closed or shaded spot keeps sending plausible numbers
that are not the field's: a pyranometer in the dark reads 0 W/m² at noon, an
anemometer in a closed space reads 0 m/s all day, and the walls damp the daily
temperature swing and keep the nights warm. ET₀, VPD and temperature alerts
built on those readings are wrong without any sensor being broken.

:func:`assess_station_exposure` compares a day of the station's hourly means
with an outdoor reference for the same place and hours (e.g. Open-Meteo) and
reports which of four signals it sees. Pure function: no I/O.

* ``no_sun``      — the pyranometer reads ~0 in daylight hours;
* ``no_wind``     — the anemometer reads 0 for the whole window while the
                    reference is windy;
* ``damped_temperature`` — the station's temperature range is under half the
                    reference range on a day with a real swing;
* ``warm_nights`` — at night the station is clearly warmer than outside.

Two or more signals → ``sheltered``; one → ``suspect``; none → ``ok``. A signal
whose sensor or reference is missing is simply not evaluated; with fewer than
``MIN_HOURS`` usable hours the status is ``insufficient_data``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import mean
from typing import Any

MIN_HOURS = 12
"""Hours with both a station and a reference temperature needed to judge."""

DAYLIGHT_REF_RS_WM2 = 200.0
"""A reference hour with at least this much shortwave counts as daylight."""
NO_SUN_MAX_WM2 = 20.0
NO_SUN_MAX_FRACTION = 0.1
"""Daylight hour where the station sees under max(20 W/m², 10 % of the
reference): the pyranometer is not seeing the sky."""
NO_SUN_MIN_DAYLIGHT_HOURS = 3
NO_SUN_SHARE = 0.8

CALM_MAX_MS = 0.1
WINDY_REF_MEAN_MS = 1.0
"""Station wind ≤ 0.1 m/s every hour while the reference (10 m) averages ≥ 1."""

REF_SWING_MIN_C = 6.0
DAMPED_RATIO = 0.5
"""Station temperature range < 50 % of the reference range, on a day whose
reference range is at least 6 °C."""

NIGHT_REF_RS_WM2 = 1.0
WARM_NIGHT_BIAS_C = 4.0
MIN_NIGHT_HOURS = 4
"""Mean (station − reference) over night hours ≥ 4 °C."""

STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"
STATUS_SHELTERED = "sheltered"
STATUS_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class ExposureHour:
    """One hour: the station's mean readings and the outdoor reference."""

    hour: datetime
    station_temp_c: float | None = None
    station_wind_ms: float | None = None
    station_rs_wm2: float | None = None
    ref_temp_c: float | None = None
    ref_wind_ms: float | None = None  # at 10 m, as weather models give it
    ref_rs_wm2: float | None = None


@dataclass(frozen=True)
class ExposureSignal:
    code: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class ExposureReport:
    status: str
    signals: list[ExposureSignal] = field(default_factory=list)
    hours_compared: int = 0

    @property
    def codes(self) -> list[str]:
        return [s.code for s in self.signals]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "hours_compared": self.hours_compared,
            "signals": [asdict(s) for s in self.signals],
        }


def _r(x: float) -> float:
    return round(x, 1)


def _no_sun(hours: Sequence[ExposureHour]) -> ExposureSignal | None:
    day = [
        h
        for h in hours
        if h.station_rs_wm2 is not None
        and h.ref_rs_wm2 is not None
        and h.ref_rs_wm2 >= DAYLIGHT_REF_RS_WM2
    ]
    if len(day) < NO_SUN_MIN_DAYLIGHT_HOURS:
        return None
    dark = [
        h for h in day if h.station_rs_wm2 < max(NO_SUN_MAX_WM2, NO_SUN_MAX_FRACTION * h.ref_rs_wm2)
    ]
    if len(dark) / len(day) < NO_SUN_SHARE:
        return None
    return ExposureSignal(
        "no_sun",
        {
            "daylight_hours": len(day),
            "dark_hours": len(dark),
            "station_max_wm2": _r(max(h.station_rs_wm2 for h in day)),
            "reference_max_wm2": _r(max(h.ref_rs_wm2 for h in day)),
        },
    )


def _no_wind(hours: Sequence[ExposureHour]) -> ExposureSignal | None:
    both = [h for h in hours if h.station_wind_ms is not None and h.ref_wind_ms is not None]
    if len(both) < MIN_HOURS:
        return None
    station_max = max(h.station_wind_ms for h in both)
    ref_mean = mean(h.ref_wind_ms for h in both)
    if station_max > CALM_MAX_MS or ref_mean < WINDY_REF_MEAN_MS:
        return None
    return ExposureSignal(
        "no_wind",
        {"hours": len(both), "station_max_ms": _r(station_max), "reference_mean_ms": _r(ref_mean)},
    )


def _damped_temperature(hours: Sequence[ExposureHour]) -> ExposureSignal | None:
    both = [h for h in hours if h.station_temp_c is not None and h.ref_temp_c is not None]
    if len(both) < MIN_HOURS:
        return None
    st = [h.station_temp_c for h in both]
    ref = [h.ref_temp_c for h in both]
    st_range, ref_range = max(st) - min(st), max(ref) - min(ref)
    if ref_range < REF_SWING_MIN_C or st_range >= DAMPED_RATIO * ref_range:
        return None
    return ExposureSignal(
        "damped_temperature",
        {
            "station_range_c": _r(st_range),
            "reference_range_c": _r(ref_range),
            "station_min_c": _r(min(st)),
            "reference_min_c": _r(min(ref)),
        },
    )


def _warm_nights(hours: Sequence[ExposureHour]) -> ExposureSignal | None:
    night = [
        h
        for h in hours
        if h.station_temp_c is not None
        and h.ref_temp_c is not None
        and h.ref_rs_wm2 is not None
        and h.ref_rs_wm2 < NIGHT_REF_RS_WM2
    ]
    if len(night) < MIN_NIGHT_HOURS:
        return None
    bias = mean(h.station_temp_c - h.ref_temp_c for h in night)
    if bias < WARM_NIGHT_BIAS_C:
        return None
    return ExposureSignal("warm_nights", {"night_hours": len(night), "mean_bias_c": _r(bias)})


def assess_station_exposure(hours: Sequence[ExposureHour]) -> ExposureReport:
    """Judge one station over a window of hourly samples (see module doc)."""
    compared = sum(1 for h in hours if h.station_temp_c is not None and h.ref_temp_c is not None)
    signals = [
        s
        for s in (_no_sun(hours), _no_wind(hours), _damped_temperature(hours), _warm_nights(hours))
        if s is not None
    ]
    if compared < MIN_HOURS and not signals:
        return ExposureReport(STATUS_INSUFFICIENT, [], compared)
    if len(signals) >= 2:
        status = STATUS_SHELTERED
    elif signals:
        status = STATUS_SUSPECT
    else:
        status = STATUS_OK
    return ExposureReport(status, signals, compared)


__all__ = [
    "ExposureHour",
    "ExposureReport",
    "ExposureSignal",
    "MIN_HOURS",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "STATUS_SHELTERED",
    "STATUS_SUSPECT",
    "assess_station_exposure",
]
