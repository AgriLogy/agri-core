"""Water level monitoring — framework-agnostic, pure (agrilogy-front#261).

Every well, basin or tank is described by a :class:`WaterSourceSpec`. Every
reading, whatever produced it, is first turned into the standard
:class:`WaterLevelReading` — water height above the bottom in metres
(``level_m``) — by a :class:`WaterLevelAdapter`. From there on, the volume,
fill percentage, usable reserve, data status and alerts depend only on
``level_m`` and the source spec, so switching a source from the simulator to a
live sensor changes the adapter and nothing else.

Formulas (developer brief v2, all SI):

* ``level_m = clamp(raw * scale + offset, 0, max_height_m)``
* ``fill_percent = 100 * level_m / max_height_m``
* rectangular: ``volume = length * width * level``
* cylindrical: ``volume = pi * (diameter / 2)**2 * level``
* irregular:   ``volume = interpolate(level, calibration_table)``
* ``available = max(volume - minimum_reserve, 0)``

Never touches the database.
"""

from __future__ import annotations

import datetime
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "ALERT_KINDS",
    "DATA_STATUSES",
    "DeviceConfig",
    "LEVEL_STATES",
    "WATER_LEVEL_ADAPTER_MAP",
    "ManualAdapter",
    "SimulatorAdapter",
    "WaterLevelAdapter",
    "WaterLevelError",
    "WaterLevelMetrics",
    "WaterLevelReading",
    "WaterSourceSpec",
    "active_alert_kinds",
    "capacity_m3",
    "compute_metrics",
    "data_status",
    "get_adapter",
    "level_state",
    "normalize_level",
    "validate_calibration_table",
    "validate_spec",
    "volume_m3",
]

LEVEL_STATES = ("NORMAL", "LOW", "CRITICAL", "HIGH", "UNKNOWN")
DATA_STATUSES = ("LIVE", "SIMULATED", "STALE", "OFFLINE", "INVALID", "CALIBRATING")
ALERT_KINDS = ("LOW", "CRITICAL", "HIGH", "OFFLINE")

# A raw value this far outside [0, max_height] (as a fraction of max_height) is
# a bad reading, not a slightly-off one: it is flagged INVALID instead of being
# silently clamped to empty or full.
INVALID_MARGIN = 0.10

# A reading older than this fraction of the offline delay is shown as STALE.
STALE_FRACTION = 0.5


class WaterLevelError(ValueError):
    """Invalid water source definition or reading payload."""


@dataclass(frozen=True)
class WaterSourceSpec:
    """What is needed to turn a level into volumes and alerts."""

    source_type: str  # BASIN | TANK | WELL
    geometry: str  # RECTANGULAR | CYLINDRICAL | IRREGULAR
    max_height_m: float
    length_m: float | None = None
    width_m: float | None = None
    diameter_m: float | None = None
    total_depth_m: float | None = None
    max_capacity_m3: float | None = None
    calibration_table: Sequence[Mapping[str, float]] | None = None
    minimum_reserve_m3: float = 0.0
    low_warning_pct: float = 30.0
    critical_pct: float = 15.0
    high_pct: float = 95.0
    offline_after_min: int = 120
    mode: str = "SIMULATED"  # SIMULATED | LIVE | CALIBRATING


@dataclass(frozen=True)
class WaterLevelReading:
    """The standard reading every adapter produces."""

    level_m: float
    measured_at: datetime.datetime
    device_id: str
    mode: str = "SIMULATED"  # SIMULATED | LIVE
    quality: str = "GOOD"  # GOOD | SUSPECT | INVALID
    battery_percent: float | None = None
    rssi: float | None = None
    raw_payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WaterLevelMetrics:
    """Everything the Water Station shows for one level."""

    level_m: float
    max_height_m: float
    fill_percent: float
    volume_m3: float | None
    capacity_m3: float | None
    free_capacity_m3: float | None
    available_m3: float | None
    minimum_reserve_m3: float
    level_state: str
    # Wells only: ground to water surface, when the total depth is known.
    depth_to_water_m: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def validate_calibration_table(
    table: Iterable[Mapping[str, Any]],
) -> list[dict[str, float]]:
    """Normalise a level/volume table: >= 2 points, sorted, both increasing."""
    points: list[dict[str, float]] = []
    for row in table:
        try:
            level = float(row["level_m"])
            volume = float(row["volume_m3"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WaterLevelError(
                "each calibration point needs numeric level_m and volume_m3"
            ) from exc
        if not (math.isfinite(level) and math.isfinite(volume)):
            raise WaterLevelError("calibration values must be finite")
        if level < 0 or volume < 0:
            raise WaterLevelError("calibration values cannot be negative")
        points.append({"level_m": level, "volume_m3": volume})
    if len(points) < 2:
        raise WaterLevelError("a calibration table needs at least 2 points")
    points.sort(key=lambda p: p["level_m"])
    for prev, cur in zip(points, points[1:], strict=False):
        if cur["level_m"] == prev["level_m"]:
            raise WaterLevelError("calibration levels must be distinct")
        if cur["volume_m3"] < prev["volume_m3"]:
            raise WaterLevelError("calibration volume must not decrease with level")
    return points


def validate_spec(spec: WaterSourceSpec) -> None:
    """Raise :class:`WaterLevelError` when the source cannot be computed."""
    if spec.source_type not in ("BASIN", "TANK", "WELL"):
        raise WaterLevelError("source_type must be BASIN, TANK or WELL")
    if spec.geometry not in ("RECTANGULAR", "CYLINDRICAL", "IRREGULAR"):
        raise WaterLevelError("geometry must be RECTANGULAR, CYLINDRICAL or IRREGULAR")
    if spec.mode not in ("SIMULATED", "LIVE", "CALIBRATING"):
        raise WaterLevelError("mode must be SIMULATED, LIVE or CALIBRATING")
    if not _positive(spec.max_height_m):
        raise WaterLevelError("max_height_m must be greater than 0")
    # Wells may omit their dimensions: volume is then simply not shown.
    is_well = spec.source_type == "WELL"
    if (
        spec.geometry == "RECTANGULAR"
        and not is_well
        and not (_positive(spec.length_m) and _positive(spec.width_m))
    ):
        raise WaterLevelError("a rectangular source needs length_m and width_m")
    if spec.geometry == "CYLINDRICAL" and not is_well and not _positive(spec.diameter_m):
        raise WaterLevelError("a cylindrical source needs diameter_m")
    if spec.geometry == "IRREGULAR":
        if not spec.calibration_table:
            raise WaterLevelError("an irregular source needs a level/volume calibration table")
        validate_calibration_table(spec.calibration_table)
    if spec.total_depth_m is not None and spec.total_depth_m < spec.max_height_m:
        raise WaterLevelError("total_depth_m cannot be less than max_height_m")
    if spec.minimum_reserve_m3 < 0:
        raise WaterLevelError("minimum_reserve_m3 cannot be negative")
    for name in ("low_warning_pct", "critical_pct", "high_pct"):
        value = getattr(spec, name)
        if not 0 <= value <= 100:
            raise WaterLevelError(f"{name} must be between 0 and 100")
    if not spec.critical_pct <= spec.low_warning_pct < spec.high_pct:
        raise WaterLevelError("thresholds must satisfy critical <= low warning < high level")
    if spec.offline_after_min <= 0:
        raise WaterLevelError("offline_after_min must be greater than 0")


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------


def normalize_level(
    raw: float, max_height_m: float, *, scale: float = 1.0, offset_m: float = 0.0
) -> tuple[float, str]:
    """``raw * scale + offset`` clamped to ``[0, max_height]``, with a quality.

    A value only slightly outside the range (sensor noise at empty / full) is
    clamped and stays GOOD; one further out than :data:`INVALID_MARGIN` of the
    height is clamped too but flagged INVALID so it never passes silently.
    """
    value = float(raw) * scale + offset_m
    if not math.isfinite(value):
        raise WaterLevelError("level is not a finite number")
    margin = INVALID_MARGIN * max_height_m
    quality = "INVALID" if value < -margin or value > max_height_m + margin else "GOOD"
    return min(max(value, 0.0), max_height_m), quality


def _interpolate(level_m: float, table: Sequence[Mapping[str, float]]) -> float:
    points = validate_calibration_table(table)
    if level_m <= points[0]["level_m"]:
        return points[0]["volume_m3"]
    if level_m >= points[-1]["level_m"]:
        return points[-1]["volume_m3"]
    for lo, hi in zip(points, points[1:], strict=False):
        if lo["level_m"] <= level_m <= hi["level_m"]:
            span = hi["level_m"] - lo["level_m"]
            t = (level_m - lo["level_m"]) / span
            return lo["volume_m3"] + t * (hi["volume_m3"] - lo["volume_m3"])
    return points[-1]["volume_m3"]  # pragma: no cover - loop always returns


def volume_m3(spec: WaterSourceSpec, level_m: float) -> float | None:
    """Water volume at ``level_m``; ``None`` when the dimensions are unknown."""
    level = min(max(level_m, 0.0), spec.max_height_m)
    if spec.geometry == "RECTANGULAR":
        if _positive(spec.length_m) and _positive(spec.width_m):
            return spec.length_m * spec.width_m * level  # type: ignore[operator]
        return None
    if spec.geometry == "CYLINDRICAL":
        if _positive(spec.diameter_m):
            return math.pi * (spec.diameter_m / 2) ** 2 * level  # type: ignore[operator]
        return None
    if spec.calibration_table:
        return _interpolate(level, spec.calibration_table)
    return None


def capacity_m3(spec: WaterSourceSpec) -> float | None:
    """Volume at the maximum height (declared capacity for irregular shapes)."""
    if spec.geometry == "IRREGULAR" and _positive(spec.max_capacity_m3):
        return spec.max_capacity_m3
    return volume_m3(spec, spec.max_height_m)


def level_state(fill_percent: float | None, spec: WaterSourceSpec) -> str:
    """NORMAL / LOW / CRITICAL / HIGH against the source thresholds."""
    if fill_percent is None:
        return "UNKNOWN"
    if fill_percent <= spec.critical_pct:
        return "CRITICAL"
    if fill_percent <= spec.low_warning_pct:
        return "LOW"
    if fill_percent >= spec.high_pct:
        return "HIGH"
    return "NORMAL"


def compute_metrics(spec: WaterSourceSpec, level_m: float) -> WaterLevelMetrics:
    """Level → fill %, volume, capacity, free capacity and usable volume."""
    level = min(max(level_m, 0.0), spec.max_height_m)
    fill = 100.0 * level / spec.max_height_m
    volume = volume_m3(spec, level)
    capacity = capacity_m3(spec)
    free = max(capacity - volume, 0.0) if capacity is not None and volume is not None else None
    available = max(volume - spec.minimum_reserve_m3, 0.0) if volume is not None else None
    depth = (
        spec.total_depth_m - level
        if spec.source_type == "WELL" and spec.total_depth_m is not None
        else None
    )
    return WaterLevelMetrics(
        level_m=level,
        max_height_m=spec.max_height_m,
        fill_percent=fill,
        volume_m3=volume,
        capacity_m3=capacity,
        free_capacity_m3=free,
        available_m3=available,
        minimum_reserve_m3=spec.minimum_reserve_m3,
        level_state=level_state(fill, spec),
        depth_to_water_m=depth,
    )


def data_status(
    spec: WaterSourceSpec,
    last: WaterLevelReading | None,
    now: datetime.datetime,
) -> str:
    """LIVE / SIMULATED / STALE / OFFLINE / INVALID / CALIBRATING.

    * no reading, or one older than ``offline_after_min`` → OFFLINE
    * source being calibrated in the field → CALIBRATING
    * last reading out of range → INVALID
    * older than half the offline delay → STALE
    * otherwise the mode of the reading (LIVE or SIMULATED)
    """
    if last is None:
        return "OFFLINE"
    age_min = (now - last.measured_at).total_seconds() / 60.0
    if age_min > spec.offline_after_min:
        return "OFFLINE"
    if spec.mode == "CALIBRATING":
        return "CALIBRATING"
    if last.quality == "INVALID":
        return "INVALID"
    if age_min > spec.offline_after_min * STALE_FRACTION:
        return "STALE"
    return "LIVE" if last.mode == "LIVE" else "SIMULATED"


def active_alert_kinds(spec: WaterSourceSpec, fill_percent: float | None, status: str) -> set[str]:
    """Which alerts should be open right now.

    OFFLINE while there is no recent data. Level alerts only from a valid,
    current level (an INVALID or stale value must not raise a level alarm);
    CRITICAL supersedes LOW.
    """
    if status == "OFFLINE":
        return {"OFFLINE"}
    if status in ("INVALID", "CALIBRATING") or fill_percent is None:
        return set()
    state = level_state(fill_percent, spec)
    return {state} if state in ("LOW", "CRITICAL", "HIGH") else set()


# ---------------------------------------------------------------------------
# Adapters: device payload → WaterLevelReading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceConfig:
    """The per-device fields an adapter may need (from water_level_device)."""

    device_id: str
    scale: float = 1.0
    offset_m: float = 0.0
    settings: Mapping[str, Any] = field(default_factory=dict)


class WaterLevelAdapter(ABC):
    """Turns one device payload into the standard reading.

    Sensor-specific decoding (Modbus registers, LoRaWAN uplinks) belongs in a
    subclass registered in :data:`WATER_LEVEL_ADAPTER_MAP`; nothing outside
    the adapter knows about the hardware.
    """

    name: str = ""
    mode: str = "LIVE"

    @abstractmethod
    def raw_level(self, payload: Mapping[str, Any]) -> float:
        """Extract the raw level value from the payload."""

    def normalize(
        self,
        payload: Mapping[str, Any],
        device: DeviceConfig,
        spec: WaterSourceSpec,
        *,
        now: datetime.datetime,
    ) -> WaterLevelReading:
        level, quality = normalize_level(
            self.raw_level(payload),
            spec.max_height_m,
            scale=device.scale,
            offset_m=device.offset_m,
        )
        measured_at = _parse_time(payload.get("measured_at")) or now
        return WaterLevelReading(
            level_m=level,
            measured_at=measured_at,
            device_id=device.device_id,
            mode=self.mode,
            quality=quality,
            battery_percent=_opt_float(payload.get("battery_percent")),
            rssi=_opt_float(payload.get("rssi")),
            raw_payload=dict(payload),
        )


class SimulatorAdapter(WaterLevelAdapter):
    """The in-app slider / mock API: ``{"level_m": 3.62}``."""

    name = "simulator"
    mode = "SIMULATED"

    def raw_level(self, payload: Mapping[str, Any]) -> float:
        return _required_float(payload, "level_m")

    def normalize(self, payload, device, spec, *, now):  # type: ignore[override]
        # The simulator sends metres already: never apply a device scale.
        reading = super().normalize(
            payload, DeviceConfig(device_id=device.device_id), spec, now=now
        )
        return replace(reading, raw_payload=None)


class ManualAdapter(WaterLevelAdapter):
    """A gateway / integration that already decoded the level in metres.

    ``{"level_m": ...}`` or a raw ``{"raw_level": ...}`` with the device's
    scale and offset applied. This is the live path until a sensor-specific
    decoder (e.g. Dragino RS485-LB) is added.
    """

    name = "manual"
    mode = "LIVE"

    def raw_level(self, payload: Mapping[str, Any]) -> float:
        if "raw_level" in payload:
            return _required_float(payload, "raw_level")
        return _required_float(payload, "level_m")


WATER_LEVEL_ADAPTER_MAP: dict[str, type[WaterLevelAdapter]] = {
    SimulatorAdapter.name: SimulatorAdapter,
    ManualAdapter.name: ManualAdapter,
}


def get_adapter(name: str) -> WaterLevelAdapter:
    try:
        return WATER_LEVEL_ADAPTER_MAP[name]()
    except KeyError as exc:
        raise WaterLevelError(f"unknown water level adapter: {name!r}") from exc


def _required_float(payload: Mapping[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise WaterLevelError(f"{key} must be a number") from exc
    if not math.isfinite(value):
        raise WaterLevelError(f"{key} must be finite")
    return value


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_time(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise WaterLevelError("measured_at must be an ISO 8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt
