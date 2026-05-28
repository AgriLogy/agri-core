"""Agronomy handler — framework-agnostic FAO-56 / doc § 3-4 logic.

Owns:

* Constants the rest of the system tunes (default Kc, RAW/TAW defaults,
  rainfall-efficiency / irrigation-efficiency / canopy-density mid-bands,
  rain-forecast trigger).
* The Dr/RAW irrigation decision (``irrigation_decision_dr``) +
  supporting struct (``IrrigationDecision``).
* The pure rainfall/depletion math (``effective_rainfall_mm``,
  ``etc_mm``, ``update_daily_depletion``, ``cumulative_dr_after_missed_days``).
* The high-level ``field_snapshot`` handler that the agri-api Celery
  notification path and any future FastAPI consumer call.

Inputs come in as plain Python dataclasses (``FieldInputs``), so the
ORM never crosses the agri-core boundary. The agri-api adapter is
responsible for translating Django models → ``FieldInputs`` and
returning the dict shape unchanged.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

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
    # Constants
    "DEFAULT_KC",
    "DEFAULT_CRITICAL_SOIL_MOISTURE_PCT",
    "DEFAULT_RAINFALL_EFFICIENCY",
    "DEFAULT_IRRIGATION_EFFICIENCY",
    "DEFAULT_CANOPY_DENSITY_KR",
    "DEFAULT_DURATION_SPLIT_THRESHOLD_HR",
    "RAIN_FORECAST_TRIGGER_MM",
    # Pure math
    "effective_rainfall_mm",
    "etc_mm",
    "update_daily_depletion",
    "cumulative_dr_after_missed_days",
    # Decision
    "IrrigationDecision",
    "irrigation_decision_dr",
    # Handler
    "ZoneParams",
    "SensorAggregates",
    "FieldInputs",
    "field_snapshot",
]
