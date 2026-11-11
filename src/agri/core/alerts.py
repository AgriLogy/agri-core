"""Alert evaluator — framework-agnostic threshold + sensor-key registry.

Owns:

* ``SENSOR_KEY_REGISTRY`` — the canonical metadata table mapping each
  string sensor key (used by the frontend charts and the Alert table) to
  its display unit, French label, type, and the *name* of the Django
  model that stores its readings. The agri-api adapter resolves that
  name to a model class; agri-core doesn't need to know what a Django
  model is.
* ``evaluate(condition, threshold, value)`` — pure threshold predicate.
* ``AlertSpec`` DTO + ``evaluate_alert(spec, value)`` — bind the
  predicate to a stored alert without leaking ORM rows into agri-core.
* ``LatestReading`` dataclass — used by the adapter to return the
  most-recent value + timestamp for an alert.
* ``suggested_alert_payload`` — pure payload assembly for the
  create-alert "prefill from recent readings" feature.

The adapter in agri-api keeps the Django-coupled pieces (Django ORM
queries, Celery dispatch, the model-string → class resolution).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# 1. Sensor-key registry
# ---------------------------------------------------------------------------

SENSOR_KEY_REGISTRY: dict[str, dict[str, Any]] = {
    "temperature_weather": {
        "model": "TemperatureWeather",
        "unit": "°C",
        "label": "Température de l'air",
        "type": "Weather Temperature",
    },
    "humidity_weather": {
        "model": "HumidityWeather",
        "unit": "%",
        "label": "Humidité de l'air",
        "type": "Humidity",
    },
    "wind_speed": {
        "model": "WindSpeed",
        "unit": "m/s",
        "label": "Vitesse du vent",
        "type": "Wind Speed",
    },
    "solar_radiation": {
        "model": "SolarRadiation",
        "unit": "W/m²",
        "label": "Rayonnement solaire",
        "type": "Weather Temperature",
    },
    "pressure_weather": {
        "model": "PressureWeather",
        "unit": "hPa",
        "label": "Pression atmosphérique",
        "type": "Pressure",
    },
    "precipitation_rate": {
        "model": "PrecipitationRate",
        "unit": "mm/h",
        "label": "Précipitations",
        "type": "Rain Fall",
    },
    "et0": {
        "model": "Et0Calculated",
        "unit": "mm/h",
        "label": "ET₀",
        "type": "Weather Temperature",
    },
    "vpd": {
        # VPDWeather rows are written by the ET₀ calc task (agri-api tasks.py).
        "model": "VPDWeather",
        "unit": "kPa",
        "label": "Déficit de pression de vapeur (DPV)",
        "type": "Weather Temperature",
    },
    "soil_moisture_medium": {
        "model": "SoilMoistureMedium",
        "unit": "%",
        "label": "Humidité du sol",
        "type": "Humidity",
    },
    "soil_temperature_medium": {
        "model": "SoilTemperatureMedium",
        "unit": "°C",
        "label": "Température du sol",
        "type": "Soil Temperature",
    },
    "soil_ph": {
        "model": "PhSoil",
        "unit": "pH",
        "label": "pH du sol",
        "type": "pH Level",
    },
    "water_flow": {
        "model": "WaterFlowSensor",
        "unit": "m³/h",
        "label": "Débit d'eau",
        "type": "Flow",
    },
    "water_pressure": {
        "model": "WaterPressureSensor",
        "unit": "bar",
        "label": "Pression d'eau",
        "type": "Pressure",
    },
    "ph_water": {
        "model": "PhWaterSensor",
        "unit": "pH",
        "label": "pH de l'eau",
        "type": "pH Level",
    },
    "water_ec": {
        "model": "WaterECSensor",
        "unit": "μS/cm",
        "label": "Conductivité de l'eau",
        "type": "EC (Electrical Conductivity)",
    },
    "soil_conductivity": {
        "model": "SoilConductivitySensor",
        "unit": "μS/cm",
        "label": "Conductivité du sol",
        "type": "EC (Electrical Conductivity)",
    },
    "soil_salinity": {
        "model": "SoilSalinitySensor",
        "unit": "mg/L",
        "label": "Salinité du sol",
        "type": "EC (Electrical Conductivity)",
    },
    "leaf_moisture": {
        "model": "LeafMoistureSensor",
        "unit": "%",
        "label": "Humidité foliaire",
        "type": "Humidity",
    },
    "leaf_temperature": {
        "model": "LeafTemperatureSensor",
        "unit": "°C",
        "label": "Température foliaire",
        "type": "Soil Temperature",
    },
    "fruit_size": {
        "model": "FruitSizeSensor",
        "unit": "mm",
        "label": "Taille des fruits",
        "type": "Periodic maintenance",
    },
    "large_fruit_diameter": {
        "model": "LargeFruitDiameterSensor",
        "unit": "mm",
        "label": "Diamètre des gros fruits",
        "type": "Periodic maintenance",
    },
    "electricity_consumption": {
        "model": "ElectricityConsumptionSensor",
        "unit": "kWh",
        "label": "Consommation électrique",
        "type": "Periodic maintenance",
    },
    # ----- Filled in to cover the rest of analytics.models sensors.
    # Convention: snake_case of the model class name, with a trailing
    # ``Sensor`` stripped. Two legacy keys (``soil_ph``, ``et0``) are kept
    # as aliases below so the frontend and existing Alert rows keep working
    # until they migrate.
    "wind_direction": {
        "model": "WindDirection",
        "unit": "°",
        "label": "Direction du vent",
        "type": "Wind Speed",
    },
    "ec_soil_low": {
        "model": "ECSoilLow",
        "unit": "μS/cm",
        "label": "EC du sol (profondeur basse)",
        "type": "EC (Electrical Conductivity)",
    },
    "ec_soil_medium": {
        "model": "ECSoilMedium",
        "unit": "μS/cm",
        "label": "EC du sol (profondeur moyenne)",
        "type": "EC (Electrical Conductivity)",
    },
    "ec_soil_high": {
        "model": "ECSoilHigh",
        "unit": "μS/cm",
        "label": "EC du sol (profondeur haute)",
        "type": "EC (Electrical Conductivity)",
    },
    "soil_moisture_low": {
        "model": "SoilMoistureLow",
        "unit": "%",
        "label": "Humidité du sol (profondeur basse)",
        "type": "Humidity",
    },
    "soil_moisture_high": {
        "model": "SoilMoistureHigh",
        "unit": "%",
        "label": "Humidité du sol (profondeur haute)",
        "type": "Humidity",
    },
    "soil_temperature_low": {
        "model": "SoilTemperatureLow",
        "unit": "°C",
        "label": "Température du sol (profondeur basse)",
        "type": "Soil Temperature",
    },
    "soil_temperature_high": {
        "model": "SoilTemperatureHigh",
        "unit": "°C",
        "label": "Température du sol (profondeur haute)",
        "type": "Soil Temperature",
    },
    "ec_salinity": {
        "model": "EcSalinitySensor",
        "unit": "dS/m",
        "label": "Salinité (EC)",
        "type": "EC (Electrical Conductivity)",
    },
    "multi_depth_soil_moisture": {
        "model": "MultiDepthSoilMoistureSensor",
        "unit": "%",
        "label": "Humidité du sol (multi-profondeur)",
        "type": "Humidity",
    },
    "water_level": {
        "model": "WaterLevelSensor",
        "unit": "m",
        "label": "Niveau d'eau",
        "type": "Flow",
    },
    "et0_weather": {
        "model": "Et0Weather",
        "unit": "mm/h",
        "label": "ET₀ (station météo)",
        "type": "Weather Temperature",
    },
    "et0_calculated": {
        "model": "Et0Calculated",
        "unit": "mm/h",
        "label": "ET₀ (calculé)",
        "type": "Weather Temperature",
    },
    # Canonical pH key matching the device's ``ph_soil`` payload. ``soil_ph``
    # stays in the registry below as a back-compat alias for existing Alert
    # rows / frontend code that hasn't migrated.
    "ph_soil": {
        "model": "PhSoil",
        "unit": "pH",
        "label": "pH du sol",
        "type": "pH Level",
    },
    # Device-health metrics — reported by LoRaWAN nodes (battery + RSSI) and,
    # for signal, by Bivocom gateways too. Surfaced per-zone like any sensor,
    # so the dashboard shows them only when a device actually reports them.
    "battery": {
        "model": "BatterySensor",
        "unit": "V",
        "label": "Batterie",
        "type": "Battery",
    },
    "signal": {
        "model": "SignalSensor",
        "unit": "dBm",
        "label": "Signal",
        "type": "Signal",
    },
}


# ---------------------------------------------------------------------------
# 2. Pure threshold predicate
# ---------------------------------------------------------------------------

GREATER_THAN = ">"
LESS_THAN = "<"
EQUAL_TO = "="

EQUALITY_TOLERANCE = 1e-3
"""Floating-point tolerance for the ``=`` comparison so sensor noise doesn't
make the predicate impossible to satisfy in practice."""


def evaluate(condition: str, threshold: float, value: float | None) -> bool:
    """Pure threshold check. Returns False when ``value`` is None so missing
    readings never fire an alert.

    >>> evaluate(">", 30, 32)
    True
    >>> evaluate(">", 30, None)
    False
    """
    if value is None:
        return False
    if condition == GREATER_THAN:
        return value > threshold
    if condition == LESS_THAN:
        return value < threshold
    if condition == EQUAL_TO:
        return abs(value - threshold) <= EQUALITY_TOLERANCE
    raise ValueError(f"Unknown alert condition: {condition!r}")


# ---------------------------------------------------------------------------
# 3. Bind to an alert spec
# ---------------------------------------------------------------------------


@dataclass
class AlertSpec:
    """Minimal alert shape the evaluator needs. Packed by the agri-api
    adapter from a Django Alert row before calling ``evaluate_alert``.
    """

    condition: str
    threshold: float


def evaluate_alert(spec: AlertSpec, value: float | None) -> bool:
    """True when ``value`` violates ``spec``'s threshold."""
    return evaluate(spec.condition, spec.threshold, value)


# ---------------------------------------------------------------------------
# 4. Latest-reading shape (filled by the adapter, consumed by callers)
# ---------------------------------------------------------------------------


@dataclass
class LatestReading:
    value: float | None
    timestamp: datetime | None


# ---------------------------------------------------------------------------
# 5. Suggest-alert prefill (pure payload assembly)
# ---------------------------------------------------------------------------


# Threshold-suggestion strategies for ``suggested_alert_payload``. ``mean`` is
# the historical default (back-compatible). ``percentile`` / ``sd`` are
# direction-aware: for a GREATER_THAN alert they bias the threshold *up* (a high
# percentile / mean+kσ) so only genuinely-high readings fire; for LESS_THAN they
# bias it *down*.
SUGGEST_STRATEGIES = ("mean", "percentile", "sd")
_PERCENTILE_HIGH = 90.0
_PERCENTILE_LOW = 10.0
_SD_K = 2.0

# Sampling windows for the suggestion. ``recent`` (default, back-compatible)
# takes the latest ``limit`` readings. ``seasonal`` takes readings from the same
# calendar window (± ``SEASONAL_WINDOW_DAYS``) in each of the previous
# ``SEASONAL_YEARS`` years, so an alert created in spring is not calibrated on
# winter readings. When that history holds fewer than
# ``SEASONAL_MIN_SAMPLES`` readings it falls back to ``recent`` and says so.
SUGGEST_WINDOWS = ("recent", "seasonal")
SEASONAL_WINDOW_DAYS = 15
SEASONAL_YEARS = 3
SEASONAL_MIN_SAMPLES = 10
SEASONAL_SAMPLE_CAP = 5000


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (``pct`` in [0, 100]) over ``values``.

    Pure + deterministic; ``numpy``-free. Assumes ``values`` is non-empty.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[int(rank)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _suggest_threshold(values: list[float], condition: str, strategy: str) -> float:
    """Compute the suggested threshold from ``values`` for the given direction
    (``condition``) and ``strategy``. Assumes ``values`` is non-empty."""
    mean = sum(values) / len(values)
    if strategy == "mean":
        return round(mean, 2)
    if strategy == "percentile":
        pct = _PERCENTILE_HIGH if condition == GREATER_THAN else _PERCENTILE_LOW
        return round(_percentile(values, pct), 2)
    # strategy == "sd": mean ± kσ (population stdev so a single sample → σ=0).
    sd = statistics.pstdev(values)
    biased = mean + _SD_K * sd if condition == GREATER_THAN else mean - _SD_K * sd
    return round(biased, 2)


def suggested_alert_payload(
    sensor_key: str,
    recent_values: list[float],
    *,
    strategy: str = "mean",
    window: str = "recent",
    seasonal_fallback: bool = False,
) -> dict[str, Any] | None:
    """Build the create-alert prefill payload from a list of recent readings.

    The adapter is responsible for fetching the recent values (most-recent
    first, ``None`` entries already filtered out); the assembly logic —
    label / unit / condition / threshold / description — lives here so the
    FastAPI rewrite can call it unchanged.

    ``strategy`` controls how the threshold is derived from ``recent_values``
    (one of :data:`SUGGEST_STRATEGIES`): ``mean`` (default, back-compatible),
    ``percentile`` (direction-aware p90/p10), or ``sd`` (mean ± 2σ,
    direction-aware). Raises ``ValueError`` for an unknown strategy.

    ``window`` names how ``recent_values`` were sampled (one of
    :data:`SUGGEST_WINDOWS`) and only changes the wording: ``seasonal`` means
    "same period in previous years". ``seasonal_fallback`` marks a seasonal
    request that had too little history and was answered from recent
    readings instead. Raises ``ValueError`` for an unknown window.

    Returns ``None`` for unknown ``sensor_key``.
    """
    if sensor_key not in SENSOR_KEY_REGISTRY:
        return None
    if strategy not in SUGGEST_STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}; expected one of {SUGGEST_STRATEGIES}.")
    if window not in SUGGEST_WINDOWS:
        raise ValueError(f"Unknown window {window!r}; expected one of {SUGGEST_WINDOWS}.")

    spec = SENSOR_KEY_REGISTRY[sensor_key]
    mean = round(sum(recent_values) / len(recent_values), 2) if recent_values else None

    # "Below mean" is the more useful default for soil moisture and for the
    # device-health metrics (low battery / weak signal are the failure modes);
    # everything else uses "above mean".
    _BELOW_MEAN_KEYS = {"battery", "signal"}
    condition = (
        LESS_THAN
        if sensor_key.startswith("soil_moisture") or sensor_key in _BELOW_MEAN_KEYS
        else GREATER_THAN
    )

    threshold = _suggest_threshold(recent_values, condition, strategy) if recent_values else 0.0
    name = f"Alerte — {spec['label']}"
    n = len(recent_values)
    if window == "seasonal":
        sample = (
            f"{n} lectures de la même période (± {SEASONAL_WINDOW_DAYS} jours) "
            "les années précédentes"
        )
    else:
        sample = f"{n} dernières lectures"
    if not recent_values:
        description = "Aucune lecture récente — ajustez le seuil manuellement."
    elif strategy == "mean":
        description = (
            f"Seuil prérempli depuis la moyenne des {sample} ({threshold} {spec['unit']})."
        )
    elif strategy == "percentile":
        pct = int(_PERCENTILE_HIGH if condition == GREATER_THAN else _PERCENTILE_LOW)
        description = (
            f"Seuil prérempli depuis le {pct}e centile des {sample} ({threshold} {spec['unit']})."
        )
    else:  # sd
        description = (
            f"Seuil prérempli depuis moyenne ± {int(_SD_K)} écarts-types des "
            f"{sample} ({threshold} {spec['unit']})."
        )
    if seasonal_fallback and recent_values:
        description += " Historique saisonnier insuffisant : lectures récentes utilisées."

    return {
        "sensor_key": sensor_key,
        "label": spec["label"],
        "unit": spec["unit"],
        "type": spec.get("type", "Weather Temperature"),
        "name": name,
        "description": description,
        "condition": condition,
        "condition_nbr": threshold,
        "mean": mean,
        "strategy": strategy,
        "window": window,
        "seasonal_fallback": seasonal_fallback,
        "sample_size": len(recent_values),
        "is_active": True,
    }


# ---------------------------------------------------------------------------
# 6. DB-backed entry points (fetch + compute over agri.db)
# ---------------------------------------------------------------------------


def db_model_for(sensor_key: str) -> type:
    """Resolve a ``sensor_key`` to its ``agri.db.analytics`` ORM model.

    The registry stores the Django model *name* (e.g. ``TemperatureWeather``);
    the matching agri.db class is ``Analytics`` + that name lowercased and
    capitalised (``AnalyticsTemperatureweather``). Raises ``KeyError`` for an
    unknown key. A test asserts every registry key resolves.
    """
    import agri.db.analytics as analytics

    name = SENSOR_KEY_REGISTRY[sensor_key]["model"]
    return getattr(analytics, "Analytics" + name.lower().capitalize())


def _latest_reading_for(
    session: Session, sensor_key: str, *, zone_id: int | None, user_id: int
) -> LatestReading:
    """Most recent reading for a sensor scoped to the zone (or the user when
    the alert has no zone). ``LatestReading(None, None)`` when unavailable."""
    from agri.core.database.client import AgriMainDBClient

    if not sensor_key or sensor_key not in SENSOR_KEY_REGISTRY:
        return LatestReading(None, None)
    model = db_model_for(sensor_key)
    row = AgriMainDBClient.latest_reading(session, model, user_id=user_id, zone_id=zone_id)
    if row is None:
        return LatestReading(None, None)
    return LatestReading(value=row.value, timestamp=row.timestamp)


def effective_zone_id_for_alert(session: Session, alert) -> int | None:
    """Resolve the farm ``zone_id`` whose reading stream feeds ``alert``.

    Three cases (agrilogy-front #57 custom notification zones):
    * Alert bound to a farm zone → that ``zone_id`` (unchanged behaviour).
    * Alert bound to a notification zone → the ``source_zone_id`` of the
      matching ``AnalyticsNotificationzonesensor`` (the assignment for the
      alert's ``sensor_key``); ``None`` when that assignment is user-wide.
    * Neither → ``None`` (user-wide latest reading).
    """
    if getattr(alert, "zone_id", None):
        return alert.zone_id
    nz_id = getattr(alert, "notification_zone_id", None)
    if not nz_id:
        return None
    from sqlalchemy import select

    from agri.db.analytics import AnalyticsNotificationzonesensor

    return session.scalars(
        select(AnalyticsNotificationzonesensor.source_zone_id)
        .where(
            AnalyticsNotificationzonesensor.notification_zone_id == nz_id,
            AnalyticsNotificationzonesensor.sensor_key == alert.sensor_key,
        )
        .order_by(AnalyticsNotificationzonesensor.id)
        .limit(1)
    ).first()


def recent_triggers_for_user(
    session: Session,
    user_id: int,
    *,
    sensor_key: str | None = None,
    zone_id: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every active alert for ``user_id`` (optionally filtered to one
    sensor/zone), annotated with its latest value, trigger state, and the
    canonical threshold for chart overlays. Ports the agri-api fan-out — the
    fetch now lives in core.

    Side effect (parity with the legacy adapter): stamps ``last_triggered_at``
    the first time an alert is found triggered. The caller owns the commit.
    """
    from sqlalchemy import select

    from agri.db.analytics import AnalyticsAlert

    now = now or datetime.now(UTC)
    criteria = [AnalyticsAlert.user_id == user_id, AnalyticsAlert.is_active.is_(True)]
    if sensor_key:
        criteria.append(AnalyticsAlert.sensor_key == sensor_key)
    if zone_id:
        criteria.append(AnalyticsAlert.zone_id == zone_id)
    rows = session.scalars(
        select(AnalyticsAlert).where(*criteria).order_by(AnalyticsAlert.id)
    ).all()

    out: list[dict[str, Any]] = []
    for alert in rows:
        latest = _latest_reading_for(
            session,
            alert.sensor_key,
            zone_id=effective_zone_id_for_alert(session, alert),
            user_id=alert.user_id,
        )
        triggered = evaluate_alert(
            AlertSpec(condition=alert.condition, threshold=float(alert.condition_nbr)),
            latest.value,
        )
        if triggered and not alert.last_triggered_at:
            alert.last_triggered_at = now
        spec = SENSOR_KEY_REGISTRY.get(alert.sensor_key, {})
        out.append(
            {
                "id": alert.id,
                "name": alert.name,
                "sensor_key": alert.sensor_key,
                "zone_id": alert.zone_id,
                "condition": alert.condition,
                "threshold": float(alert.condition_nbr),
                "unit": spec.get("unit"),
                "label": spec.get("label"),
                "is_active": alert.is_active,
                "latest_value": latest.value,
                "latest_timestamp": (latest.timestamp.isoformat() if latest.timestamp else None),
                "is_triggered": triggered,
                "last_triggered_at": (
                    alert.last_triggered_at.isoformat() if alert.last_triggered_at else None
                ),
            }
        )
    return out


def _years_back(day: date, years: int) -> date:
    """``day`` moved back ``years`` calendar years (29 Feb → 28 Feb)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def seasonal_ranges(
    today: date,
    *,
    years: int = SEASONAL_YEARS,
    window_days: int = SEASONAL_WINDOW_DAYS,
) -> list[tuple[datetime, datetime]]:
    """Half-open UTC ``[start, end)`` ranges covering ``today`` ± ``window_days``
    in each of the previous ``years`` years, most recent year first."""
    out: list[tuple[datetime, datetime]] = []
    for k in range(1, years + 1):
        center = _years_back(today, k)
        start = center - timedelta(days=window_days)
        end = center + timedelta(days=window_days + 1)
        out.append(
            (
                datetime(start.year, start.month, start.day, tzinfo=UTC),
                datetime(end.year, end.month, end.day, tzinfo=UTC),
            )
        )
    return out


def suggest_alert_for(
    session: Session,
    user_id: int,
    sensor_key: str,
    *,
    zone_id: int | None = None,
    limit: int = 20,
    strategy: str = "mean",
    window: str = "recent",
    today: date | None = None,
) -> dict[str, Any] | None:
    """Fetch readings for ``sensor_key`` (scoped to the zone, or the user) and
    build the create-alert prefill via :func:`suggested_alert_payload` (using
    ``strategy`` for the threshold).

    ``window="recent"`` samples the latest ``limit`` readings.
    ``window="seasonal"`` samples every reading (capped at
    :data:`SEASONAL_SAMPLE_CAP`) within ± :data:`SEASONAL_WINDOW_DAYS` of
    ``today``'s date in each of the previous :data:`SEASONAL_YEARS` years; with
    fewer than :data:`SEASONAL_MIN_SAMPLES` such readings it falls back to the
    recent sample and sets ``seasonal_fallback``. ``None`` for an unknown
    ``sensor_key``; ``ValueError`` for an unknown strategy or window."""
    from sqlalchemy import and_, or_, select

    from agri.core.database.client import (
        _owner_user,
        _owner_zone,
        _with_device_join,
    )

    if sensor_key not in SENSOR_KEY_REGISTRY:
        return None
    if window not in SUGGEST_WINDOWS:
        raise ValueError(f"Unknown window {window!r}; expected one of {SUGGEST_WINDOWS}.")
    model = db_model_for(sensor_key)
    # Resolve ownership via the device JOIN so a device's history follows it.
    owner = _owner_zone(model) == zone_id if zone_id else _owner_user(model) == user_id
    base = _with_device_join(select(model.value), model).where(owner, model.value.is_not(None))

    if window == "seasonal":
        ranges = seasonal_ranges(today or datetime.now(UTC).date())
        in_season = or_(*(and_(model.timestamp >= lo, model.timestamp < hi) for lo, hi in ranges))
        seasonal = session.scalars(
            base.where(in_season).order_by(model.timestamp.desc()).limit(SEASONAL_SAMPLE_CAP)
        ).all()
        if len(seasonal) >= SEASONAL_MIN_SAMPLES:
            return suggested_alert_payload(
                sensor_key, [float(v) for v in seasonal], strategy=strategy, window="seasonal"
            )

    recent = session.scalars(base.order_by(model.timestamp.desc()).limit(limit)).all()
    return suggested_alert_payload(
        sensor_key,
        [float(v) for v in recent],
        strategy=strategy,
        window="recent",
        seasonal_fallback=window == "seasonal",
    )


__all__ = [
    "SENSOR_KEY_REGISTRY",
    "GREATER_THAN",
    "LESS_THAN",
    "EQUAL_TO",
    "EQUALITY_TOLERANCE",
    "AlertSpec",
    "LatestReading",
    "evaluate",
    "evaluate_alert",
    "suggested_alert_payload",
    "SUGGEST_STRATEGIES",
    "SUGGEST_WINDOWS",
    "seasonal_ranges",
    "db_model_for",
    "effective_zone_id_for_alert",
    "recent_triggers_for_user",
    "suggest_alert_for",
]
