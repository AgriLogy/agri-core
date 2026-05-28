"""Agronomy handler — framework-agnostic FAO-56 / doc § 3-4 logic.

Owns:

* Constants the rest of the system tunes (default Kc, RAW/TAW defaults,
  rainfall-efficiency / irrigation-efficiency / canopy-density mid-bands,
  rain-forecast trigger, FAO-56 physical constants).
* The pure FAO-56 hourly math: saturation vapor pressure, slope, ψ,
  vapor-pressure deficit, solar geometry / Ra, net radiation, ASCE
  hourly Penman-Monteith.
* The Dr/RAW irrigation decision (``irrigation_decision_dr``) +
  supporting struct (``IrrigationDecision``).
* The pure rainfall/depletion math (``effective_rainfall_mm``,
  ``etc_mm``, ``update_daily_depletion``, ``cumulative_dr_after_missed_days``).
* Two high-level handlers:
    - ``compute_zone_et0(inputs)`` — one hour of ET₀ + VPD for a zone.
    - ``field_snapshot(inputs)``    — daily status dict for the
      notification email.

Inputs come in as plain Python dataclasses, so the ORM never crosses
the agri-core boundary. The agri-api adapter is responsible for
translating Django models → DTOs.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import acos, cos, exp, log, pi, radians, sin, sqrt, tan
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants — kept in sync with the agronomist's spec (back/agriBack/agronomy.py
# header notes prior to the lift). Tunable per deployment in the future via
# a settings DTO; today they are hard-coded so the math stays referenceable.
# ---------------------------------------------------------------------------

DEFAULT_KC = 1.0
"""Crop coefficient when nothing better is configured."""

DEFAULT_CRITICAL_SOIL_MOISTURE_PCT = 20.0
"""Soil-moisture % below which we recommend irrigation in the legacy branch."""

DEFAULT_RAINFALL_EFFICIENCY = 0.8
"""α — fraction of rainfall stored in the root zone (FAO-56 effective rain)."""

DEFAULT_IRRIGATION_EFFICIENCY = 0.85
"""Net→gross irrigation efficiency (drip ≈ 0.9, sprinkler ≈ 0.75; pick mid)."""

DEFAULT_CANOPY_DENSITY_KR = 1.0
"""Kr — canopy density coefficient. 1.0 = full cover."""

DEFAULT_DURATION_SPLIT_THRESHOLD_HR = 4.0
"""Single-shot duration above which the daily volume is split morning + evening."""

RAIN_FORECAST_TRIGGER_MM = 2.0
"""Rain forecast above this triggers the doc § 4.2 rain branch."""

_DECISION_PHRASE = {
    "stress": "Irrigation recommandée — Dr ≥ RAW (zone de stress)",
    "soil_moisture_low": "Irrigation recommandée — humidité du sol sous le seuil",
    "complementary": "Irrigation complémentaire — pluie prévue insuffisante",
    "rain_will_suffice": "Pas d'irrigation — la pluie prévue suffira",
    "no_stress": "Pas d'irrigation requise — pas d'état de stress",
}

# ---------------------------------------------------------------------------
# FAO-56 physical constants (used by the hourly Penman-Monteith math)
# ---------------------------------------------------------------------------

ALBEDO_SHORT_CROP = 0.23
"""FAO-56 reference grass albedo."""

SIGMA = 2.043e-10
"""Stefan-Boltzmann in HOURLY MJ units (Annex 4 eq. 39).

The legacy code used 4.903e-9 (the DAILY value), which overestimated
longwave radiation by 24×, drove Rn negative, and silently clamped ET₀
to 0 even at noon. Tests guard against regressing to the daily value.
"""

SOLAR_CONSTANT_MJ_M2_MIN = 0.0820
"""FAO-56 solar constant for the hourly Ra computation (eq. 28)."""

LST_DEG_MOROCCO = 15.0
"""Morocco standard meridian (UTC+1 permanent since 2018, east-positive)."""

DEPLOYMENT_LOCAL_TZ = ZoneInfo("Africa/Casablanca")
"""Deployment local time zone — for solar-time conversion in
``compute_zone_et0``. Future per-site deployments can pass an
explicit tz to ``Et0Inputs`` once the field is added."""

CLOUD_RATIO_MIN = 0.3
CLOUD_RATIO_MAX = 1.0
"""Bounds on Rs/Rso, per the 2026-05-10 agronomist review."""

CLOUD_FACTOR_MIN = 0.05
"""Floor on 1.35*ratio - 0.35; guards future callers that pass non-physical input."""

CROP_STAGE_PROFILES: dict[str, dict[str, float]] = {
    "emergence":         {"zr_m": 0.10, "taw_mm": 18.0,  "raw_mm": 9.0},
    "early_vegetative":  {"zr_m": 0.25, "taw_mm": 45.0,  "raw_mm": 22.5},
    "vegetative_growth": {"zr_m": 0.45, "taw_mm": 81.0,  "raw_mm": 40.5},
    "flowering":         {"zr_m": 0.70, "taw_mm": 126.0, "raw_mm": 63.0},
    "fruit_filling":     {"zr_m": 1.00, "taw_mm": 180.0, "raw_mm": 90.0},
}
"""Doc § 2.2 crop-stage → effective root depth + TAW + RAW. Choosing the
right stage at runtime is deferred (TODO: per-zone configuration)."""


# ---------------------------------------------------------------------------
# Pure math (FAO-56 hourly)
# ---------------------------------------------------------------------------


def saturation_vapor_pressure_kpa(temp_c: float) -> float:
    """es(T) — Tetens, FAO-56 eq. 11."""
    return 0.6108 * exp((17.27 * temp_c) / (temp_c + 237.3))


def slope_svp_kpa_per_c(temp_c: float) -> float:
    """Δ — slope of the saturation vapor-pressure curve (FAO-56 eq. 13)."""
    es = saturation_vapor_pressure_kpa(temp_c)
    return 4098.0 * es / ((temp_c + 237.3) ** 2)


def psychrometric_constant_kpa_per_c(pressure_kpa: float) -> float:
    """γ ≈ 0.000665 · P, FAO-56 eq. 8 (λ ≈ 2.45 MJ/kg)."""
    return 0.000665 * pressure_kpa


def wperm2_to_mjm2_per_hour(wperm2: float) -> float:
    """Mean W/m² → hourly MJ/m²/h."""
    return wperm2 * 0.0036


def actual_vapor_pressure_kpa(temp_c: float, rh_pct: float) -> float:
    """ea = es(T) * RH/100, with RH clamped to [1, 100] %.

    The 1% floor is the agronomist's explicit ask (review 2026-05-10):
    a sensor reading 0% RH is physically impossible and drives ea=0,
    which inflates VPD and ET₀.
    """
    rh = max(1.0, min(100.0, rh_pct))
    return saturation_vapor_pressure_kpa(temp_c) * (rh / 100.0)


def wind_speed_at_2m(u_z_ms: float, sensor_height_m: float = 2.0) -> float:
    """Project measured wind speed at ``sensor_height_m`` down to 2 m (FAO-56 eq. 47)."""
    if sensor_height_m == 2.0:
        return max(0.0, u_z_ms)
    return max(0.0, u_z_ms * 4.87 / log(67.8 * sensor_height_m - 5.42))


def equation_of_time_minutes(day_of_year: int) -> float:
    """EoT in minutes (Spencer, FAO-56 Annex 2)."""
    b = 2.0 * pi * (day_of_year - 81) / 364.0
    return 9.87 * sin(2.0 * b) - 7.53 * cos(b) - 1.5 * sin(b)


def solar_time_correction_hours(
    day_of_year: int,
    lon_deg: float,
    lst_deg: float = LST_DEG_MOROCCO,
) -> float:
    """Local civil time → local solar time, in hours.

    FAO-56 writes the longitude term as 4*(Lst - Lloc) with both
    longitudes positive WEST of Greenwich. This codebase carries
    longitudes east-positive (matching the GIS convention), so the
    equivalent formula is 4*(lon_deg - lst_deg). The equivalence is
    documented so the discrepancy with the source text is not flagged
    in a future review.
    """
    return (4.0 * (lon_deg - lst_deg) + equation_of_time_minutes(day_of_year)) / 60.0


def extraterrestrial_radiation_hourly_mjm2h(
    lat_deg: float,
    lon_deg: float,
    day_of_year: int,
    local_clock_hour: float,
    lst_deg: float = LST_DEG_MOROCCO,
) -> float:
    """Hourly Ra (MJ/m²/h) for the hour centered on ``local_clock_hour``. FAO-56 eq. 28."""
    phi = radians(lat_deg)
    declination = 0.409 * sin(2.0 * pi * day_of_year / 365.0 - 1.39)
    dr = 1.0 + 0.033 * cos(2.0 * pi * day_of_year / 365.0)

    sunset_arg = max(-1.0, min(1.0, -tan(phi) * tan(declination)))
    omega_s = acos(sunset_arg)

    t_solar = local_clock_hour + solar_time_correction_hours(
        day_of_year, lon_deg, lst_deg
    )
    omega = (pi / 12.0) * (t_solar - 12.0)
    omega1 = omega - pi / 24.0
    omega2 = omega + pi / 24.0

    if omega2 <= -omega_s or omega1 >= omega_s:
        return 0.0

    omega1 = max(omega1, -omega_s)
    omega2 = min(omega2, omega_s)

    return (
        (12.0 * 60.0 / pi)
        * SOLAR_CONSTANT_MJ_M2_MIN
        * dr
        * (
            (omega2 - omega1) * sin(phi) * sin(declination)
            + cos(phi) * cos(declination) * (sin(omega2) - sin(omega1))
        )
    )


def vpd_kpa(temp_c: float, rh_pct: float) -> float:
    """Vapor-pressure deficit, never negative."""
    es = saturation_vapor_pressure_kpa(temp_c)
    ea = actual_vapor_pressure_kpa(temp_c, rh_pct)
    return max(0.0, es - ea)


def cloudiness_ratio(rs_mjm2h: float, rso_mjm2h: float) -> float:
    """Rs/Rso clamped to [CLOUD_RATIO_MIN, CLOUD_RATIO_MAX]."""
    if rso_mjm2h <= 0.0:
        return CLOUD_RATIO_MIN
    return max(CLOUD_RATIO_MIN, min(CLOUD_RATIO_MAX, rs_mjm2h / rso_mjm2h))


def net_radiation_mjm2h(
    rs_mjm2h: float,
    ea_kpa: float,
    temp_c: float,
    *,
    ra_mjm2h: float | None = None,
    elevation_m: float = 0.0,
) -> float:
    """FAO-56 hourly Rn. When Ra is provided the cloudiness ratio uses
    the [0.3, 1.0] clamp; otherwise we fall back to a 0.75 mid-band heuristic.
    """
    rns = (1 - ALBEDO_SHORT_CROP) * rs_mjm2h
    tk = temp_c + 273.16
    emiss_term = 0.34 - 0.14 * sqrt(max(0.0, ea_kpa))

    if ra_mjm2h is not None and ra_mjm2h > 0:
        rso = (0.75 + 2e-5 * elevation_m) * ra_mjm2h
        rs_over_rso = cloudiness_ratio(rs_mjm2h, rso)
    else:
        rs_over_rso = 0.75

    cloud_term = max(CLOUD_FACTOR_MIN, 1.35 * rs_over_rso - 0.35)
    rnl = SIGMA * (tk**4) * emiss_term * cloud_term
    return rns - rnl


def soil_heat_flux_mjm2h(rn_mjm2h: float, *, daytime: bool) -> float:
    """ASCE/FAO hourly G ≈ 0.1·Rn day, 0.5·Rn night."""
    return 0.1 * rn_mjm2h if daytime else 0.5 * rn_mjm2h


def asce_hourly_short_crop_coeffs(daytime: bool) -> tuple[float, float]:
    """ASCE standardized hourly reference (short crop): (Cn, Cd)."""
    return (37.0, 0.24 if daytime else 0.96)


def is_daytime(rs_mjm2h: float) -> bool:
    """Daytime when Rs > 0. Using Rs (not Rn) avoids flipping the regime
    on cool humid days where the longwave overshoot briefly drives Rn < 0.
    """
    return rs_mjm2h > 0.0


def penman_monteith_hourly_mm(
    *,
    temp_c: float,
    rh_pct: float,
    wind_ms: float,
    pressure_kpa: float,
    rs_mjm2h: float,
    ra_mjm2h: float | None = None,
    elevation_m: float = 0.0,
    wind_height_m: float = 2.0,
) -> dict[str, float]:
    """One hour of FAO-56 Penman-Monteith ET₀ (mm/h) and VPD.

    Returns a dict with intermediates so callers / tests can inspect
    them. ``ra_mjm2h`` should be supplied (via
    ``extraterrestrial_radiation_hourly_mjm2h``) whenever lat/lon are
    known; with None the function falls back to the 0.75 heuristic.
    """
    es = saturation_vapor_pressure_kpa(temp_c)
    ea = actual_vapor_pressure_kpa(temp_c, rh_pct)
    delta = slope_svp_kpa_per_c(temp_c)
    gamma = psychrometric_constant_kpa_per_c(pressure_kpa)

    rn = net_radiation_mjm2h(
        rs_mjm2h, ea, temp_c, ra_mjm2h=ra_mjm2h, elevation_m=elevation_m
    )
    daytime = is_daytime(rs_mjm2h)
    g = soil_heat_flux_mjm2h(rn, daytime=daytime)
    cn, cd = asce_hourly_short_crop_coeffs(daytime)

    u2 = wind_speed_at_2m(wind_ms, wind_height_m)
    numerator = 0.408 * delta * (rn - g) + gamma * (cn / (temp_c + 273.0)) * u2 * (
        es - ea
    )
    denominator = delta + gamma * (1.0 + cd * u2)
    et0_mm_per_h = max(0.0, numerator / max(denominator, 1e-6))

    return {
        "et0_mm_per_h": et0_mm_per_h,
        "vpd_kpa": max(0.0, es - ea),
        "delta": delta,
        "gamma": gamma,
        "rn_mjm2h": rn,
        "g_mjm2h": g,
        "daytime": daytime,
        "u2_ms": u2,
    }


# ---------------------------------------------------------------------------
# Pure water-balance math (FAO-56 / doc § 3-4)
# ---------------------------------------------------------------------------


def effective_rainfall_mm(
    rain_mm: float, alpha: float = DEFAULT_RAINFALL_EFFICIENCY
) -> float:
    """Pe = α · P — the fraction of rainfall that reaches the root zone."""
    return max(0.0, alpha * rain_mm)


def etc_mm(
    et0_mm: float,
    kc: float = DEFAULT_KC,
    *,
    permeability_loss_mm: float = 0.0,
) -> float:
    """ETc_adj = ET0 · Kc + permeability_loss_mm  (doc § 4.1)."""
    return max(0.0, et0_mm * kc + permeability_loss_mm)


def update_daily_depletion(
    *,
    dr_yesterday_mm: float,
    etc_today_mm: float,
    pe_today_mm: float,
    irrigation_applied_mm: float,
) -> float:
    """Dr,i = max(0, Dr,(i-1) + ETc - Pe - In)  (FAO-56 / doc § 3.1)."""
    return max(
        0.0,
        dr_yesterday_mm + etc_today_mm - pe_today_mm - irrigation_applied_mm,
    )


def cumulative_dr_after_missed_days(
    *,
    dr_baseline_mm: float,
    et0_per_day_mm: Iterable[float],
    rain_per_day_mm: Iterable[float],
    kc: float = DEFAULT_KC,
    alpha: float = DEFAULT_RAINFALL_EFFICIENCY,
    permeability_loss_mm: float = 0.0,
) -> float:
    """Doc § 4.3 catch-up: roll Dr forward over a sequence of skipped days."""
    et0_list = list(et0_per_day_mm)
    rain_list = list(rain_per_day_mm)
    etc_cumul = sum(
        etc_mm(e, kc, permeability_loss_mm=permeability_loss_mm) for e in et0_list
    )
    pe_cumul = sum(effective_rainfall_mm(r, alpha) for r in rain_list)
    effective = max(0.0, etc_cumul - pe_cumul)
    return max(0.0, dr_baseline_mm + effective)


# ---------------------------------------------------------------------------
# Irrigation decision (Dr/RAW branch)
# ---------------------------------------------------------------------------


@dataclass
class IrrigationDecision:
    """Result of ``irrigation_decision_dr``. See ``reason`` for the branch."""

    irrigate: bool
    # One of: "stress", "soil_moisture_low", "no_stress", "rain_will_suffice",
    # "complementary".
    reason: str
    net_mm: float                          # In_net — net depth to bring to FC
    gross_mm: float                        # Ig — net / efficiency × Kr
    volume_m3: float                       # gross applied to zone area
    duration_hr: float                     # volume / flow_rate
    morning_volume_m3: float | None        # split when duration > threshold
    evening_volume_m3: float | None
    capped_to_daily_max: bool              # True if volume hit max_water_per_day


def _build_irrigation_struct(
    *,
    reason: str,
    net_mm: float,
    zone_area_m2: float,
    irrigation_efficiency: float,
    kr: float,
    flow_rate_m3h: float,
    max_water_per_day_m3: float,
    duration_split_threshold_hr: float,
) -> IrrigationDecision:
    """Net depth → gross depth → m³ → duration, with daily cap + morning/evening split."""
    if net_mm <= 0.0:
        return IrrigationDecision(
            irrigate=False,
            reason=reason,
            net_mm=0.0,
            gross_mm=0.0,
            volume_m3=0.0,
            duration_hr=0.0,
            morning_volume_m3=None,
            evening_volume_m3=None,
            capped_to_daily_max=False,
        )

    gross_mm = (net_mm * kr) / max(irrigation_efficiency, 1e-6)
    volume_m3 = gross_mm * zone_area_m2 / 1000.0

    capped = False
    if max_water_per_day_m3 > 0 and volume_m3 > max_water_per_day_m3:
        volume_m3 = max_water_per_day_m3
        capped = True

    duration_hr = volume_m3 / max(flow_rate_m3h, 1e-6)

    morning = evening = None
    if duration_hr > duration_split_threshold_hr:
        morning = evening = volume_m3 / 2.0

    return IrrigationDecision(
        irrigate=True,
        reason=reason,
        net_mm=net_mm,
        gross_mm=gross_mm,
        volume_m3=volume_m3,
        duration_hr=duration_hr,
        morning_volume_m3=morning,
        evening_volume_m3=evening,
        capped_to_daily_max=capped,
    )


def irrigation_decision_dr(
    *,
    dr_today_mm: float,
    raw_mm: float,
    soil_moisture_pct: float | None,
    critical_moisture_pct: float,
    zone_area_m2: float,
    flow_rate_m3h: float,
    max_water_per_day_m3: float = 0.0,
    irrigation_efficiency: float = DEFAULT_IRRIGATION_EFFICIENCY,
    kr: float = DEFAULT_CANOPY_DENSITY_KR,
    duration_split_threshold_hr: float = DEFAULT_DURATION_SPLIT_THRESHOLD_HR,
    precipitation_forecast_mm: float = 0.0,
    alpha_rain: float = DEFAULT_RAINFALL_EFFICIENCY,
) -> IrrigationDecision:
    """Dr/RAW-based irrigation decision (doc § 4.1 + § 4.2).

    Trigger logic:

    * Dr >= RAW                                      → stress       → irrigate
    * soil_moisture < critical_moisture_pct          → soil_moisture_low → irrigate
    * rain forecast > 2 mm AND Dr <  RAW             → rain_will_suffice → suspend
    * rain forecast > 2 mm AND Dr >= RAW             → complementary → irrigate Dr − Pe
    * otherwise                                      → no_stress    → suspend

    Net depth follows § 4.1: In_net = Dr_today (bring soil back to FC), then
    gross = In_net · Kr / efficiency, volume = gross · area / 1000, capped
    at max_water_per_day, split morning/evening if duration > threshold.
    """
    soil_stressed = dr_today_mm >= raw_mm
    soil_dry = (
        soil_moisture_pct is not None
        and soil_moisture_pct < critical_moisture_pct
    )

    if precipitation_forecast_mm > RAIN_FORECAST_TRIGGER_MM:
        pe_forecast = effective_rainfall_mm(precipitation_forecast_mm, alpha_rain)
        if not soil_stressed:
            return IrrigationDecision(
                irrigate=False,
                reason="rain_will_suffice",
                net_mm=0.0,
                gross_mm=0.0,
                volume_m3=0.0,
                duration_hr=0.0,
                morning_volume_m3=None,
                evening_volume_m3=None,
                capped_to_daily_max=False,
            )
        return _build_irrigation_struct(
            reason="complementary",
            net_mm=max(0.0, dr_today_mm - pe_forecast),
            zone_area_m2=zone_area_m2,
            irrigation_efficiency=irrigation_efficiency,
            kr=kr,
            flow_rate_m3h=flow_rate_m3h,
            max_water_per_day_m3=max_water_per_day_m3,
            duration_split_threshold_hr=duration_split_threshold_hr,
        )

    if not (soil_stressed or soil_dry):
        return IrrigationDecision(
            irrigate=False,
            reason="no_stress",
            net_mm=0.0,
            gross_mm=0.0,
            volume_m3=0.0,
            duration_hr=0.0,
            morning_volume_m3=None,
            evening_volume_m3=None,
            capped_to_daily_max=False,
        )

    return _build_irrigation_struct(
        reason="stress" if soil_stressed else "soil_moisture_low",
        net_mm=max(0.0, dr_today_mm),
        zone_area_m2=zone_area_m2,
        irrigation_efficiency=irrigation_efficiency,
        kr=kr,
        flow_rate_m3h=flow_rate_m3h,
        max_water_per_day_m3=max_water_per_day_m3,
        duration_split_threshold_hr=duration_split_threshold_hr,
    )


def _format_decision(
    et0_kc_mm: float | None,
    soil_moisture_pct: float | None,
    *,
    decision: IrrigationDecision | None = None,
) -> str:
    """One-line French summary of the irrigation recommendation."""
    if decision is not None:
        head = _DECISION_PHRASE.get(decision.reason, decision.reason)
        if decision.irrigate:
            tail = (
                f" — {decision.volume_m3:.2f} m³ "
                f"(~{decision.duration_hr * 60:.0f} min)."
            )
            if decision.capped_to_daily_max:
                tail += " Plafonné à max_water_per_day."
            return head + tail
        return head + "."

    if et0_kc_mm is None or soil_moisture_pct is None:
        return "Données insuffisantes — recommandation indisponible."

    if soil_moisture_pct < DEFAULT_CRITICAL_SOIL_MOISTURE_PCT:
        return (
            f"Irrigation recommandée maintenant — humidité du sol "
            f"{soil_moisture_pct:.0f} % < seuil "
            f"{DEFAULT_CRITICAL_SOIL_MOISTURE_PCT:.0f} %, "
            f"ETo×Kc ≈ {et0_kc_mm:.2f} mm."
        )

    return (
        f"Pas d'irrigation requise — humidité du sol "
        f"{soil_moisture_pct:.0f} % au-dessus du seuil, "
        f"ETo×Kc ≈ {et0_kc_mm:.2f} mm."
    )


# ---------------------------------------------------------------------------
# compute_zone_et0 handler — one hour of ET₀ + VPD for a zone
# ---------------------------------------------------------------------------


@dataclass
class Et0Inputs:
    """One hour of weather averages plus zone identity, in sensor units.

    The adapter is responsible for fetching/averaging readings and
    packing them here; the handler does the FAO-56 math.
    ``rs_wm2`` arrives in W/m² and ``pressure_hpa`` in hPa; the
    handler does the unit conversions to MJ/m²/h and kPa internally
    so the caller doesn't have to.
    """

    zone_id: int
    user_id: int
    timestamp: datetime
    """End of the hour window the inputs cover. Returned on ``ZoneEt0``."""
    temp_c: float | None
    rh_pct: float | None
    wind_ms: float | None
    rs_wm2: float | None
    pressure_hpa: float | None
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float = 0.0
    wind_height_m: float = 2.0


@dataclass
class ZoneEt0:
    """Per-zone ET₀ / VPD result of ``compute_zone_et0``."""

    zone_id: int
    user_id: int
    timestamp: datetime
    et0_mm_per_h: float
    vpd_kpa: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp.isoformat(),
            "et0_mm_per_h": self.et0_mm_per_h,
            "vpd_kpa": self.vpd_kpa,
        }


def compute_zone_et0(inputs: Et0Inputs) -> ZoneEt0 | None:
    """One hour of FAO-56 Penman-Monteith ET₀ + VPD for the zone.

    Returns ``None`` if any of the required weather inputs is missing
    for the slot (sensor outage / no data). When ``latitude`` and
    ``longitude`` are supplied, the extraterrestrial radiation Ra is
    computed for the midpoint of the hour window using local civil
    time at ``DEPLOYMENT_LOCAL_TZ``; otherwise net radiation falls
    back to the 0.75 mid-band heuristic.
    """
    if any(
        v is None
        for v in (
            inputs.temp_c,
            inputs.rh_pct,
            inputs.wind_ms,
            inputs.rs_wm2,
            inputs.pressure_hpa,
        )
    ):
        return None

    ra_mjm2h: float | None = None
    if inputs.latitude is not None and inputs.longitude is not None:
        midpoint_local = (
            inputs.timestamp - timedelta(minutes=30)
        ).astimezone(DEPLOYMENT_LOCAL_TZ)
        ra_mjm2h = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=inputs.latitude,
            lon_deg=inputs.longitude,
            day_of_year=midpoint_local.timetuple().tm_yday,
            local_clock_hour=midpoint_local.hour
            + midpoint_local.minute / 60.0,
        )

    result = penman_monteith_hourly_mm(
        temp_c=inputs.temp_c,
        rh_pct=inputs.rh_pct,
        wind_ms=inputs.wind_ms,
        pressure_kpa=inputs.pressure_hpa * 0.1,
        rs_mjm2h=wperm2_to_mjm2_per_hour(inputs.rs_wm2),
        ra_mjm2h=ra_mjm2h,
        elevation_m=inputs.elevation_m,
        wind_height_m=inputs.wind_height_m,
    )

    return ZoneEt0(
        zone_id=inputs.zone_id,
        user_id=inputs.user_id,
        timestamp=inputs.timestamp,
        et0_mm_per_h=result["et0_mm_per_h"],
        vpd_kpa=result["vpd_kpa"],
    )


# ---------------------------------------------------------------------------
# field_snapshot handler — high-level entry point used by notifications
# ---------------------------------------------------------------------------


@dataclass
class ZoneParams:
    """The agronomy-relevant subset of a Zone, packed by the agri-api adapter."""

    name: str | None
    area_m2: float | None                       # zone.space
    raw_mm: float | None                        # zone.soil_param_RAW
    taw_mm: float | None                        # zone.soil_param_TAW
    pomp_flow_rate_l_per_s: float | None        # zone.pomp_flow_rate (L/s)
    irrigation_water_quantity_l: float | None   # zone.irrigation_water_quantity (L)
    critical_moisture_pct: float | None         # zone.critical_moisture_threshold


@dataclass
class SensorAggregates:
    """Aggregated sensor readings the snapshot reports on. None = no data."""

    yesterday_temp_c: float | None
    today_temp_c: float | None
    yesterday_humidity_pct: float | None
    today_humidity_pct: float | None
    et0_today_mm: float | None
    soil_moisture_pct: float | None
    soil_temperature_c: float | None
    soil_ph: float | None
    soil_ec: float | None
    soil_salinity: float | None
    npk_n: float | None
    npk_p: float | None
    npk_k: float | None
    last_irrigation_at: datetime | None
    last_irrigation_l: float | None


@dataclass
class FieldInputs:
    """Everything ``field_snapshot`` needs. The adapter packs this from Django."""

    date_today: date
    zone: ZoneParams | None
    sensors: SensorAggregates | None
    dr_today_mm: float | None = None
    precipitation_forecast_mm: float = 0.0


_NO_ZONE_MESSAGE = (
    "Aucune zone configurée pour ce compte — créez une zone pour"
    " activer les recommandations."
)


def _empty_snapshot(date_today: date, *, irrigation_decision: str) -> dict[str, Any]:
    """The "no data yet" shape — returned when the user has no zone."""
    return {
        "zone_name": None,
        "date_today": date_today,
        "yesterday_temp_c": None,
        "today_temp_c": None,
        "yesterday_humidity_pct": None,
        "today_humidity_pct": None,
        "et0_today_mm": None,
        "soil_moisture_pct": None,
        "soil_temperature_c": None,
        "soil_ph": None,
        "soil_ec": None,
        "soil_salinity": None,
        "npk_n": None,
        "npk_p": None,
        "npk_k": None,
        "last_irrigation_at": None,
        "last_irrigation_l": None,
        "perfect_irrigation_window": "06:00 – 07:00",
        "kc_used": DEFAULT_KC,
        "irrigation_decision": irrigation_decision,
        "dr_today_mm": None,
        "raw_mm": None,
        "taw_mm": None,
        "decision_reason": None,
        "recommended_volume_m3": None,
        "recommended_duration_min": None,
        "morning_volume_m3": None,
        "evening_volume_m3": None,
    }


def field_snapshot(inputs: FieldInputs) -> dict[str, Any]:
    """Render the daily status dict consumed by the notification email.

    The return shape is a dict (not a dataclass) because the email
    template indexes keys directly; keeping it a dict avoids a churny
    callsite migration. Future revisions may wrap this in a typed DTO
    once the template no longer reaches into it.
    """
    if inputs.zone is None or inputs.sensors is None:
        return _empty_snapshot(
            inputs.date_today, irrigation_decision=_NO_ZONE_MESSAGE
        )

    zone = inputs.zone
    sensors = inputs.sensors

    kc_used = DEFAULT_KC
    et0_kc_mm = (
        sensors.et0_today_mm * kc_used
        if sensors.et0_today_mm is not None
        else None
    )

    decision: IrrigationDecision | None = None
    if (
        inputs.dr_today_mm is not None
        and zone.raw_mm
        and zone.area_m2
        and zone.pomp_flow_rate_l_per_s
    ):
        flow_rate_m3h = zone.pomp_flow_rate_l_per_s * 3.6
        max_water_per_day_m3 = (
            (zone.irrigation_water_quantity_l or 0.0) / 1000.0
            if zone.irrigation_water_quantity_l is not None
            else 0.0
        )
        critical_pct = (
            zone.critical_moisture_pct
            if zone.critical_moisture_pct is not None
            else DEFAULT_CRITICAL_SOIL_MOISTURE_PCT
        )
        decision = irrigation_decision_dr(
            dr_today_mm=inputs.dr_today_mm,
            raw_mm=zone.raw_mm,
            soil_moisture_pct=sensors.soil_moisture_pct,
            critical_moisture_pct=critical_pct,
            zone_area_m2=zone.area_m2,
            flow_rate_m3h=flow_rate_m3h,
            max_water_per_day_m3=max_water_per_day_m3,
            precipitation_forecast_mm=inputs.precipitation_forecast_mm,
        )

    return {
        "zone_name": zone.name,
        "date_today": inputs.date_today,
        "yesterday_temp_c": sensors.yesterday_temp_c,
        "today_temp_c": sensors.today_temp_c,
        "yesterday_humidity_pct": sensors.yesterday_humidity_pct,
        "today_humidity_pct": sensors.today_humidity_pct,
        "et0_today_mm": sensors.et0_today_mm,
        "soil_moisture_pct": sensors.soil_moisture_pct,
        "soil_temperature_c": sensors.soil_temperature_c,
        "soil_ph": sensors.soil_ph,
        "soil_ec": sensors.soil_ec,
        "soil_salinity": sensors.soil_salinity,
        "npk_n": sensors.npk_n,
        "npk_p": sensors.npk_p,
        "npk_k": sensors.npk_k,
        "last_irrigation_at": sensors.last_irrigation_at,
        "last_irrigation_l": sensors.last_irrigation_l,
        # TODO(expert): derive from solar / morning vs. evening windows.
        "perfect_irrigation_window": "06:00 – 07:00",
        "kc_used": kc_used,
        "irrigation_decision": _format_decision(
            et0_kc_mm, sensors.soil_moisture_pct, decision=decision
        ),
        "dr_today_mm": inputs.dr_today_mm,
        "raw_mm": zone.raw_mm,
        "taw_mm": zone.taw_mm,
        "decision_reason": decision.reason if decision else None,
        "recommended_volume_m3": decision.volume_m3 if decision else None,
        "recommended_duration_min": (
            decision.duration_hr * 60.0 if decision else None
        ),
        "morning_volume_m3": decision.morning_volume_m3 if decision else None,
        "evening_volume_m3": decision.evening_volume_m3 if decision else None,
    }


__all__ = [
    # Doc § 3-4 constants
    "DEFAULT_KC",
    "DEFAULT_CRITICAL_SOIL_MOISTURE_PCT",
    "DEFAULT_RAINFALL_EFFICIENCY",
    "DEFAULT_IRRIGATION_EFFICIENCY",
    "DEFAULT_CANOPY_DENSITY_KR",
    "DEFAULT_DURATION_SPLIT_THRESHOLD_HR",
    "RAIN_FORECAST_TRIGGER_MM",
    "CROP_STAGE_PROFILES",
    # FAO-56 physical constants
    "ALBEDO_SHORT_CROP",
    "SIGMA",
    "SOLAR_CONSTANT_MJ_M2_MIN",
    "LST_DEG_MOROCCO",
    "DEPLOYMENT_LOCAL_TZ",
    "CLOUD_RATIO_MIN",
    "CLOUD_RATIO_MAX",
    "CLOUD_FACTOR_MIN",
    # FAO-56 hourly math
    "saturation_vapor_pressure_kpa",
    "slope_svp_kpa_per_c",
    "psychrometric_constant_kpa_per_c",
    "wperm2_to_mjm2_per_hour",
    "actual_vapor_pressure_kpa",
    "wind_speed_at_2m",
    "equation_of_time_minutes",
    "solar_time_correction_hours",
    "extraterrestrial_radiation_hourly_mjm2h",
    "vpd_kpa",
    "cloudiness_ratio",
    "net_radiation_mjm2h",
    "soil_heat_flux_mjm2h",
    "asce_hourly_short_crop_coeffs",
    "is_daytime",
    "penman_monteith_hourly_mm",
    # Pure water-balance math
    "effective_rainfall_mm",
    "etc_mm",
    "update_daily_depletion",
    "cumulative_dr_after_missed_days",
    # Irrigation decision
    "IrrigationDecision",
    "irrigation_decision_dr",
    # ET₀ handler
    "Et0Inputs",
    "ZoneEt0",
    "compute_zone_et0",
    # Field-snapshot handler
    "ZoneParams",
    "SensorAggregates",
    "FieldInputs",
    "field_snapshot",
]
