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

The report also gives a verdict per sensor (:attr:`ExposureReport.sensors`):
``solar`` (from ``no_sun``), ``wind`` (``no_wind``) and ``temperature``
(``damped_temperature`` / ``warm_nights``) are each ``sheltered`` when one of
their signals fired, ``ok`` when they were checked and none fired, and
``not_checked`` when the data could not judge them (sensor missing, overcast
day, too few hours).
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

SENSOR_OK = "ok"
SENSOR_SHELTERED = "sheltered"
SENSOR_NOT_CHECKED = "not_checked"

SENSOR_SIGNALS: dict[str, tuple[str, ...]] = {
    "solar": ("no_sun",),
    "wind": ("no_wind",),
    "temperature": ("damped_temperature", "warm_nights"),
}
"""Which signals judge which sensor."""


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
    checked: list[str] = field(default_factory=list)
    """Signal codes the data could judge (fired or not)."""

    @property
    def codes(self) -> list[str]:
        return [s.code for s in self.signals]

    @property
    def sensors(self) -> dict[str, str]:
        """Per-sensor verdict: ``ok`` / ``sheltered`` / ``not_checked``."""
        fired, checked = set(self.codes), set(self.checked)
        out = {}
        for sensor, codes in SENSOR_SIGNALS.items():
            if fired & set(codes):
                out[sensor] = SENSOR_SHELTERED
            elif checked & set(codes):
                out[sensor] = SENSOR_OK
            else:
                out[sensor] = SENSOR_NOT_CHECKED
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "hours_compared": self.hours_compared,
            "signals": [asdict(s) for s in self.signals],
            "checked": list(self.checked),
            "sensors": self.sensors,
        }


def _r(x: float) -> float:
    return round(x, 1)


_Judged = tuple[bool, ExposureSignal | None]
"""(the data could judge this signal, the signal if it fired)."""


def _no_sun(hours: Sequence[ExposureHour]) -> _Judged:
    day = [
        h
        for h in hours
        if h.station_rs_wm2 is not None
        and h.ref_rs_wm2 is not None
        and h.ref_rs_wm2 >= DAYLIGHT_REF_RS_WM2
    ]
    if len(day) < NO_SUN_MIN_DAYLIGHT_HOURS:
        return False, None
    dark = [
        h for h in day if h.station_rs_wm2 < max(NO_SUN_MAX_WM2, NO_SUN_MAX_FRACTION * h.ref_rs_wm2)
    ]
    if len(dark) / len(day) < NO_SUN_SHARE:
        return True, None
    return True, ExposureSignal(
        "no_sun",
        {
            "daylight_hours": len(day),
            "dark_hours": len(dark),
            "station_max_wm2": _r(max(h.station_rs_wm2 for h in day)),
            "reference_max_wm2": _r(max(h.ref_rs_wm2 for h in day)),
        },
    )


def _no_wind(hours: Sequence[ExposureHour]) -> _Judged:
    both = [h for h in hours if h.station_wind_ms is not None and h.ref_wind_ms is not None]
    if len(both) < MIN_HOURS:
        return False, None
    station_max = max(h.station_wind_ms for h in both)
    ref_mean = mean(h.ref_wind_ms for h in both)
    if ref_mean < WINDY_REF_MEAN_MS:
        return False, None  # a calm day can't tell a sheltered anemometer apart
    if station_max > CALM_MAX_MS:
        return True, None
    return True, ExposureSignal(
        "no_wind",
        {"hours": len(both), "station_max_ms": _r(station_max), "reference_mean_ms": _r(ref_mean)},
    )


def _damped_temperature(hours: Sequence[ExposureHour]) -> _Judged:
    both = [h for h in hours if h.station_temp_c is not None and h.ref_temp_c is not None]
    if len(both) < MIN_HOURS:
        return False, None
    st = [h.station_temp_c for h in both]
    ref = [h.ref_temp_c for h in both]
    st_range, ref_range = max(st) - min(st), max(ref) - min(ref)
    if ref_range < REF_SWING_MIN_C:
        return False, None  # no real swing outside: nothing to compare
    if st_range >= DAMPED_RATIO * ref_range:
        return True, None
    return True, ExposureSignal(
        "damped_temperature",
        {
            "station_range_c": _r(st_range),
            "reference_range_c": _r(ref_range),
            "station_min_c": _r(min(st)),
            "reference_min_c": _r(min(ref)),
        },
    )


def _warm_nights(hours: Sequence[ExposureHour]) -> _Judged:
    night = [
        h
        for h in hours
        if h.station_temp_c is not None
        and h.ref_temp_c is not None
        and h.ref_rs_wm2 is not None
        and h.ref_rs_wm2 < NIGHT_REF_RS_WM2
    ]
    if len(night) < MIN_NIGHT_HOURS:
        return False, None
    bias = mean(h.station_temp_c - h.ref_temp_c for h in night)
    if bias < WARM_NIGHT_BIAS_C:
        return True, None
    return True, ExposureSignal("warm_nights", {"night_hours": len(night), "mean_bias_c": _r(bias)})


def assess_station_exposure(hours: Sequence[ExposureHour]) -> ExposureReport:
    """Judge one station over a window of hourly samples (see module doc)."""
    compared = sum(1 for h in hours if h.station_temp_c is not None and h.ref_temp_c is not None)
    judged = {
        code: fn(hours)
        for code, fn in (
            ("no_sun", _no_sun),
            ("no_wind", _no_wind),
            ("damped_temperature", _damped_temperature),
            ("warm_nights", _warm_nights),
        )
    }
    checked = [code for code, (ok, _) in judged.items() if ok]
    signals = [sig for _, sig in judged.values() if sig is not None]
    if compared < MIN_HOURS and not signals:
        return ExposureReport(STATUS_INSUFFICIENT, [], compared, checked)
    if len(signals) >= 2:
        status = STATUS_SHELTERED
    elif signals:
        status = STATUS_SUSPECT
    else:
        status = STATUS_OK
    return ExposureReport(status, signals, compared, checked)


__all__ = [
    "ExposureHour",
    "ExposureReport",
    "ExposureSignal",
    "MIN_HOURS",
    "SENSOR_NOT_CHECKED",
    "SENSOR_OK",
    "SENSOR_SHELTERED",
    "SENSOR_SIGNALS",
    "STATUS_INSUFFICIENT",
    "STATUS_OK",
    "STATUS_SHELTERED",
    "STATUS_SUSPECT",
    "assess_station_exposure",
]
