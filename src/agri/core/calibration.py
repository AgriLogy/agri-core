"""Sensor calibration + unit conversion — framework-agnostic, pure.

Business-logic half of CAL-1 (agri-web #67 / agri-core #68). Storage
lives in ``agri-db`` as ``analytics_sensorcalibration``, one row per
``(device_id, sensor_key)`` holding ``scale_a``, ``offset_b``, ``unit``
and ``is_active``. This module owns the *math* over that row; it never
touches the database and never rewrites stored readings.

Owns:

* ``Calibration`` DTO — the framework-agnostic shape of a calibration
  row (``from_row`` duck-types any ORM/pydantic object with the same
  attribute names, so no ``agri.db`` import is needed).
* ``apply_calibration`` / ``invert_calibration`` — the affine correction
  ``real = raw * scale_a + offset_b`` and its exact inverse.
* ``UNIT_DIMENSIONS`` — the unit table: each physical dimension lists
  its units as an affine map to that dimension's base unit. Any pair
  inside a dimension is derived from those two maps, so **adding a unit
  is one line in the table and touches no calibration logic**.
* ``UNIT_CONVERSION_OVERRIDES`` — the escape hatch for non-standard or
  sensor-specific pairs (see "Known gap" below).
* ``conversion_coefficients`` / ``convert_value`` — the linear unit
  conversion itself.
* ``calibration_in_unit`` — the piece the ticket actually cares about:
  re-express a calibration in a different target unit by recomputing
  ``scale_a`` and ``offset_b`` deterministically.

**Known gap.** The referenced ``change.the.unite.of.sensors…1.3.pdf`` is
unavailable, so any non-standard or vendor-specific conversion it
specifies is *not* in this table. Only conversions justifiable from
physics are declared here. When the spec surfaces, add the missing pairs
to ``UNIT_CONVERSION_OVERRIDES`` (or a new dimension in
``UNIT_DIMENSIONS``) — the calibration functions stay unchanged.

**Fail loudly.** An unknown unit, or a pair spanning two dimensions,
raises. Silently returning the uncorrected value would put a wrong
number in front of a farmer, which is the worst possible outcome.

Applying corrections in the read path (dashboard / alerts / reports) is
deliberately *not* here: that must happen at a single point so the three
surfaces cannot diverge. It is the follow-up ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agri.core.alerts import SENSOR_KEY_REGISTRY

# ---------------------------------------------------------------------------
# 1. Errors
# ---------------------------------------------------------------------------


class CalibrationError(ValueError):
    """Base class for every calibration / unit-conversion failure."""


class UnknownUnitError(CalibrationError):
    """Raised when a unit string is absent from the unit table."""


class UnknownUnitConversionError(CalibrationError):
    """Raised when both units are known but no conversion between them exists
    (different physical dimensions, or a dimension with no defined map)."""


class NonInvertibleCalibrationError(CalibrationError):
    """Raised when inverting a calibration whose ``scale_a`` is 0 — the
    forward map collapses every raw value onto one point, so no inverse
    exists."""


# ---------------------------------------------------------------------------
# 2. The unit table
# ---------------------------------------------------------------------------
#
# Each dimension declares a *base* unit and, for every unit in it, the affine
# map to that base:
#
#     value_base = value_unit * to_base_scale + to_base_offset
#
# Two maps are enough to convert any pair inside a dimension (see
# ``conversion_coefficients``), so the table stays O(n) instead of O(n²) and a
# new unit is a single line. Units the platform reads but that have no
# meaningful conversion partner (``%``, ``pH``, ``°``, ``V``, ``dBm``) are
# listed in single-unit dimensions on purpose: they resolve as *known* units
# whose only legal target is themselves, so a bogus request such as
# ``pH -> °C`` raises instead of silently succeeding.
#
# ``dBm`` deserves a note: dBm→mW is logarithmic (mW = 10**(dBm/10)), not
# affine, so it cannot be expressed here and is not offered. Composing a
# non-linear conversion with an affine calibration would not be affine, which
# would break the whole storage model.


@dataclass(frozen=True)
class UnitDefinition:
    """One unit's affine map to its dimension's base unit."""

    to_base_scale: float
    to_base_offset: float = 0.0


@dataclass(frozen=True)
class Dimension:
    """A physical dimension: a base unit plus every unit expressed in it."""

    base: str
    units: dict[str, UnitDefinition]


UNIT_DIMENSIONS: dict[str, Dimension] = {
    # --- Temperature: base kelvin. °C and °F carry a real offset; this is the
    # only dimension in the table where ``to_base_offset`` is non-zero.
    "temperature": Dimension(
        base="K",
        units={
            "K": UnitDefinition(1.0),
            "°C": UnitDefinition(1.0, 273.15),
            # K = (°F + 459.67) * 5/9 = °F * 5/9 + 255.3722…
            "°F": UnitDefinition(5.0 / 9.0, 459.67 * 5.0 / 9.0),
        },
    ),
    # --- Pressure: base kPa (the unit VPD is stored in).
    "pressure": Dimension(
        base="kPa",
        units={
            "kPa": UnitDefinition(1.0),
            "Pa": UnitDefinition(0.001),
            "hPa": UnitDefinition(0.1),
            "mbar": UnitDefinition(0.1),
            "bar": UnitDefinition(100.0),
            "MPa": UnitDefinition(1000.0),
            "psi": UnitDefinition(6.894757293168361),  # NIST: 1 psi = 6894.757 Pa
            "atm": UnitDefinition(101.325),
        },
    ),
    # --- Electrical conductivity: base µS/cm (registry default for EC keys).
    # 1 mS/cm = 1 dS/m = 1000 µS/cm; 1 S/m = 10 mS/cm.
    "conductivity": Dimension(
        base="μS/cm",
        units={
            "μS/cm": UnitDefinition(1.0),
            "mS/cm": UnitDefinition(1000.0),
            "dS/m": UnitDefinition(1000.0),
            "S/m": UnitDefinition(10000.0),
        },
    ),
    # --- Length / depth: base mm (fruit size, water level, rainfall totals).
    "length": Dimension(
        base="mm",
        units={
            "mm": UnitDefinition(1.0),
            "cm": UnitDefinition(10.0),
            "m": UnitDefinition(1000.0),
            "in": UnitDefinition(25.4),  # exact by definition
            "ft": UnitDefinition(304.8),
        },
    ),
    # --- Speed: base m/s (wind).
    "speed": Dimension(
        base="m/s",
        units={
            "m/s": UnitDefinition(1.0),
            "km/h": UnitDefinition(1.0 / 3.6),
            "mph": UnitDefinition(0.44704),  # exact: 1609.344 m / 3600 s
            "kn": UnitDefinition(1852.0 / 3600.0),  # nautical mile per hour
        },
    ),
    # --- Depth rate: base mm/h (precipitation, ET₀).
    "depth_rate": Dimension(
        base="mm/h",
        units={
            "mm/h": UnitDefinition(1.0),
            "mm/day": UnitDefinition(1.0 / 24.0),
            "cm/h": UnitDefinition(10.0),
            "in/h": UnitDefinition(25.4),
            "in/day": UnitDefinition(25.4 / 24.0),
        },
    ),
    # --- Volumetric flow: base m³/h (water-flow sensors).
    "volume_flow": Dimension(
        base="m³/h",
        units={
            "m³/h": UnitDefinition(1.0),
            "m³/s": UnitDefinition(3600.0),
            "L/h": UnitDefinition(0.001),
            "L/min": UnitDefinition(0.06),
            "L/s": UnitDefinition(3.6),
        },
    ),
    # --- Mass concentration: base mg/L (soil salinity).
    # ppm ≡ mg/L holds for dilute aqueous solutions (density ≈ 1 kg/L), which
    # is the only regime these probes report in.
    "mass_concentration": Dimension(
        base="mg/L",
        units={
            "mg/L": UnitDefinition(1.0),
            "ppm": UnitDefinition(1.0),
            "μg/L": UnitDefinition(0.001),
            "g/L": UnitDefinition(1000.0),
            "ppt": UnitDefinition(1000.0),  # parts per thousand
        },
    ),
    # --- Irradiance: base W/m² (solar radiation).
    # MJ/m²/h is an hourly energy total; dividing by 3600 s gives the mean
    # power over that hour, so 1 MJ/m²/h = 1e6/3600 W/m².
    "irradiance": Dimension(
        base="W/m²",
        units={
            "W/m²": UnitDefinition(1.0),
            "kW/m²": UnitDefinition(1000.0),
            "MJ/m²/h": UnitDefinition(1.0e6 / 3600.0),
        },
    ),
    # --- Energy: base kWh (electricity consumption).
    "energy": Dimension(
        base="kWh",
        units={
            "kWh": UnitDefinition(1.0),
            "Wh": UnitDefinition(0.001),
            "MWh": UnitDefinition(1000.0),
            "MJ": UnitDefinition(1.0 / 3.6),  # 1 kWh = 3.6 MJ
            "J": UnitDefinition(1.0 / 3.6e6),
        },
    ),
    # --- Electric potential: base V (LoRaWAN battery).
    "voltage": Dimension(
        base="V",
        units={
            "V": UnitDefinition(1.0),
            "mV": UnitDefinition(0.001),
        },
    ),
    # --- Single-unit dimensions: known, but convertible only to themselves.
    "ratio": Dimension(base="%", units={"%": UnitDefinition(1.0)}),
    "acidity": Dimension(base="pH", units={"pH": UnitDefinition(1.0)}),
    "angle": Dimension(base="°", units={"°": UnitDefinition(1.0)}),
    "signal_power": Dimension(base="dBm", units={"dBm": UnitDefinition(1.0)}),
}


UNIT_ALIASES: dict[str, str] = {
    # MICRO SIGN (U+00B5) vs GREEK SMALL LETTER MU (U+03BC): both are typed in
    # the wild; SENSOR_KEY_REGISTRY uses the greek mu, so that is canonical.
    "µS/cm": "μS/cm",
    "uS/cm": "μS/cm",
    "us/cm": "μS/cm",
    "µg/L": "μg/L",
    "ug/L": "μg/L",
    "C": "°C",
    "degC": "°C",
    "celsius": "°C",
    "F": "°F",
    "degF": "°F",
    "fahrenheit": "°F",
    "kelvin": "K",
    "inch": "in",
    "inches": "in",
    "kph": "km/h",
    "km/hr": "km/h",
    "knot": "kn",
    "kt": "kn",
    "m3/h": "m³/h",
    "m3/s": "m³/s",
    "W/m2": "W/m²",
    "kW/m2": "kW/m²",
    "l/h": "L/h",
    "l/min": "L/min",
    "l/s": "L/s",
    "percent": "%",
    "pct": "%",
    "deg": "°",
    "mm/d": "mm/day",
    "in/d": "in/day",
}
"""Spelling variants → the canonical unit string used in ``UNIT_DIMENSIONS``.

Extension point #1: a new spelling costs one line here."""


UNIT_CONVERSION_OVERRIDES: dict[tuple[str, str], tuple[float, float]] = {}
"""Explicit ``(from, to) -> (scale, offset)`` pairs that the dimension table
cannot express — vendor-specific or sensor-specific conversions.

Extension point #2, and the placeholder for whatever
``change.the.unite.of.sensors…1.3.pdf`` turns out to specify. Entries here win
over the dimension table, and the conversion stays affine
(``target = source * scale + offset``) so the composition rule in
:func:`calibration_in_unit` still holds. Nothing in the calibration logic
needs to change to honour a new entry."""


# ---------------------------------------------------------------------------
# 3. Unit conversion
# ---------------------------------------------------------------------------


def normalize_unit(unit: str | None) -> str:
    """Canonicalise a unit string: trim whitespace and resolve aliases.

    Returns ``""`` for ``None`` / blank, which by the storage convention means
    "keep the ``SENSOR_KEY_REGISTRY`` default for this sensor key".

    >>> normalize_unit(" uS/cm ")
    'μS/cm'
    """
    if unit is None:
        return ""
    trimmed = unit.strip()
    if not trimmed:
        return ""
    return UNIT_ALIASES.get(trimmed, trimmed)


def unit_dimension(unit: str) -> str | None:
    """Name of the dimension ``unit`` belongs to, or ``None`` if unknown."""
    canonical = normalize_unit(unit)
    for name, dimension in UNIT_DIMENSIONS.items():
        if canonical in dimension.units:
            return name
    return None


def is_known_unit(unit: str) -> bool:
    """True when ``unit`` appears in the unit table (after alias resolution)."""
    return unit_dimension(unit) is not None


def conversion_coefficients(from_unit: str, to_unit: str) -> tuple[float, float]:
    """Affine coefficients ``(m, c)`` such that ``value_to = value_from * m + c``.

    Derivation — inside a dimension every unit is declared by its map to the
    base unit ``B``::

        base = from * s_f + o_f          (definition of `from`)
        base = to   * s_t + o_t          (definition of `to`)

    Eliminating ``base`` and solving for ``to``::

        to = (from * s_f + o_f - o_t) / s_t
           = from * (s_f / s_t) + (o_f - o_t) / s_t

    hence ``m = s_f / s_t`` and ``c = (o_f - o_t) / s_t``. Both are constants,
    so the conversion is itself affine — which is what makes composing it with
    an affine calibration closed (see :func:`calibration_in_unit`).

    Identity is returned when the two units are the same string, even for a
    unit outside the table: "express this in the unit it is already in" is
    always well-defined and must not raise.

    Raises :class:`UnknownUnitError` for a unit absent from the table and
    :class:`UnknownUnitConversionError` for a cross-dimension pair. Neither is
    ever swallowed: a silently-unconverted value is a wrong number shown to a
    farmer.

    ``conversion_coefficients("°C", "°F")`` returns ``(1.8, 32.0)`` up to
    float precision.
    """
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)

    if source == target:
        return (1.0, 0.0)

    override = UNIT_CONVERSION_OVERRIDES.get((source, target))
    if override is not None:
        return override

    source_dim = unit_dimension(source)
    target_dim = unit_dimension(target)
    if source_dim is None:
        raise UnknownUnitError(f"Unknown unit {from_unit!r}; not in the agri-core unit table.")
    if target_dim is None:
        raise UnknownUnitError(f"Unknown unit {to_unit!r}; not in the agri-core unit table.")
    if source_dim != target_dim:
        raise UnknownUnitConversionError(
            f"No conversion from {source!r} ({source_dim}) to {target!r} ({target_dim}): "
            "different physical dimensions."
        )

    from_def = UNIT_DIMENSIONS[source_dim].units[source]
    to_def = UNIT_DIMENSIONS[target_dim].units[target]
    scale = from_def.to_base_scale / to_def.to_base_scale
    offset = (from_def.to_base_offset - to_def.to_base_offset) / to_def.to_base_scale
    return (scale, offset)


def convert_value(value: float | None, from_unit: str, to_unit: str) -> float | None:
    """Convert one reading between units. ``None`` in → ``None`` out (a missing
    reading stays missing; it is never coerced to 0).

    ``convert_value(25.0, "°C", "°F")`` → ``77.0`` (up to float precision).
    """
    if value is None:
        return None
    scale, offset = conversion_coefficients(from_unit, to_unit)
    return value * scale + offset


# ---------------------------------------------------------------------------
# 4. The calibration itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Calibration:
    """Affine correction of one sensor stream: ``real = raw * scale_a + offset_b``.

    Mirrors ``analytics_sensorcalibration`` field-for-field, minus the keys —
    the caller already knows which ``(device_id, sensor_key)`` it fetched.

    ``unit`` is the unit ``real`` is expressed in; ``""`` means "the
    ``SENSOR_KEY_REGISTRY`` default for this sensor key" (see
    :func:`effective_unit`). ``is_active=False`` disables the correction
    entirely — :func:`apply_calibration` then returns the raw value untouched.
    """

    scale_a: float = 1.0
    offset_b: float = 0.0
    unit: str = ""
    is_active: bool = True

    @property
    def is_identity(self) -> bool:
        """True when the correction leaves every value unchanged."""
        return self.scale_a == 1.0 and self.offset_b == 0.0

    @classmethod
    def from_row(cls, row: Any) -> Calibration:
        """Build from anything exposing the storage attribute names — an
        ``AnalyticsSensorcalibration`` ORM row, a pydantic model, a stub.
        Duck-typed on purpose: agri-core stays import-free of the ORM here,
        and missing attributes fall back to the column defaults.
        """
        scale_a = getattr(row, "scale_a", None)
        offset_b = getattr(row, "offset_b", None)
        is_active = getattr(row, "is_active", None)
        return cls(
            scale_a=1.0 if scale_a is None else float(scale_a),
            offset_b=0.0 if offset_b is None else float(offset_b),
            unit=normalize_unit(getattr(row, "unit", "")),
            is_active=True if is_active is None else bool(is_active),
        )


IDENTITY_CALIBRATION = Calibration()
"""The no-op calibration: what an un-calibrated sensor behaves like."""


def effective_unit(sensor_key: str, calibration: Calibration | None = None) -> str:
    """Unit a corrected reading is expressed in.

    The calibration's ``unit`` wins; blank falls back to the
    ``SENSOR_KEY_REGISTRY`` default. Returns ``""`` for an unknown sensor key
    with no calibration unit — the caller decides whether that is an error, so
    nothing is invented here.
    """
    if calibration is not None:
        unit = normalize_unit(calibration.unit)
        if unit:
            return unit
    spec = SENSOR_KEY_REGISTRY.get(sensor_key)
    if spec is None:
        return ""
    return normalize_unit(str(spec.get("unit", "")))


def apply_calibration(raw: float | None, calibration: Calibration | None) -> float | None:
    """Apply ``real = raw * scale_a + offset_b``.

    Behaviour at the edges, all deliberate:

    * ``raw is None`` → ``None``. A missing reading stays missing; turning it
      into ``offset_b`` would fabricate data.
    * ``calibration is None`` → ``raw``. No row stored = no correction.
    * ``is_active is False`` → ``raw``. The correction is disabled, not deleted.
    * identity (``a=1, b=0``) → ``raw`` unchanged, with no float round-trip.

    >>> apply_calibration(10.0, Calibration(scale_a=2.0, offset_b=1.0))
    21.0
    """
    if raw is None:
        return None
    if calibration is None or not calibration.is_active or calibration.is_identity:
        return raw
    return raw * calibration.scale_a + calibration.offset_b


def invert_calibration(real: float | None, calibration: Calibration | None) -> float | None:
    """Recover the raw reading from a corrected one: ``raw = (real - b) / a``.

    Exact inverse of :func:`apply_calibration` (subject to float precision),
    with the same ``None`` / inactive / identity short-circuits. Raises
    :class:`NonInvertibleCalibrationError` when ``scale_a == 0``.
    """
    if real is None:
        return None
    if calibration is None or not calibration.is_active or calibration.is_identity:
        return real
    if calibration.scale_a == 0.0:
        raise NonInvertibleCalibrationError(
            "Cannot invert a calibration with scale_a == 0: the forward map is not injective."
        )
    return (real - calibration.offset_b) / calibration.scale_a


# ---------------------------------------------------------------------------
# 5. Calibration ∘ unit conversion — the composition rule
# ---------------------------------------------------------------------------


def calibration_in_unit(
    calibration: Calibration,
    target_unit: str,
    *,
    sensor_key: str | None = None,
) -> Calibration:
    """Re-express ``calibration`` so its corrected value comes out in
    ``target_unit``, recomputing ``scale_a`` and ``offset_b`` deterministically.

    Derivation — the calibration is affine in the current unit ``u₁``::

        real_u₁ = raw * a + b

    and the unit conversion ``u₁ → u₂`` is affine too (see
    :func:`conversion_coefficients`)::

        v_u₂ = v_u₁ * m + c

    Substituting the first into the second::

        real_u₂ = (raw * a + b) * m + c
                = raw * (a * m) + (b * m + c)

    which is again of the form ``raw * a' + b'`` — the composition of two
    affine maps is affine — with::

        a' = a * m
        b' = b * m + c

    So a unit change is a closed-form rewrite of the two stored coefficients;
    no reading is ever converted twice and nothing has to be recomputed from
    history. Note ``b`` is scaled *and* shifted while ``a`` is only scaled:
    the offset is expressed in the target unit, so °C→°F turns
    ``(a, b)`` into ``(1.8a, 1.8b + 32)``.

    The source unit is :func:`effective_unit` — the calibration's own unit, or
    the ``SENSOR_KEY_REGISTRY`` default for ``sensor_key`` when it is blank.
    Raises :class:`UnknownUnitError` if the source unit cannot be determined
    (blank calibration unit and no/unknown ``sensor_key``): guessing would risk
    a silently wrong scale factor.

    An inactive calibration is retargeted the same way — the coefficients are
    kept meaningful for when it is switched back on — while
    :func:`apply_calibration` keeps ignoring them.
    """
    source_unit = effective_unit(sensor_key or "", calibration)
    if not source_unit:
        raise UnknownUnitError(
            "Cannot determine the calibration's current unit: it is blank and "
            f"sensor_key={sensor_key!r} has no registry default. Set Calibration.unit "
            "or pass a known sensor_key."
        )
    scale, offset = conversion_coefficients(source_unit, target_unit)
    return replace(
        calibration,
        scale_a=calibration.scale_a * scale,
        offset_b=calibration.offset_b * scale + offset,
        unit=normalize_unit(target_unit),
    )


def calibrated_value(
    raw: float | None,
    calibration: Calibration | None,
    *,
    sensor_key: str | None = None,
    target_unit: str | None = None,
) -> float | None:
    """Corrected reading, optionally expressed in ``target_unit``.

    The single composed entry point: apply the affine correction, then — if a
    target unit is asked for and differs from the calibration's own — apply the
    conversion. Equivalent to ``apply_calibration`` against
    ``calibration_in_unit(...)``, and tested to agree with it.

    With no ``calibration`` and a ``target_unit``, the raw value is converted
    from the sensor's registry unit; that still requires a resolvable source
    unit and raises otherwise.
    """
    corrected = apply_calibration(raw, calibration)
    if corrected is None or not target_unit:
        return corrected

    source_unit = effective_unit(sensor_key or "", calibration)
    if not source_unit:
        raise UnknownUnitError(
            "Cannot determine the reading's current unit: the calibration unit is "
            f"blank and sensor_key={sensor_key!r} has no registry default."
        )
    return convert_value(corrected, source_unit, target_unit)


def supported_units_for(sensor_key: str) -> list[str]:
    """Units a given sensor key can be displayed in — every unit sharing a
    dimension with its registry default, sorted for a stable UI ordering.

    Returns ``[]`` for an unknown sensor key or a default unit that is not in
    the table (rather than raising): this feeds a picker, not a computation.
    """
    default = effective_unit(sensor_key)
    if not default:
        return []
    dimension = unit_dimension(default)
    if dimension is None:
        return []
    return sorted(UNIT_DIMENSIONS[dimension].units)


__all__ = [
    # Errors
    "CalibrationError",
    "UnknownUnitError",
    "UnknownUnitConversionError",
    "NonInvertibleCalibrationError",
    # Unit table
    "UnitDefinition",
    "Dimension",
    "UNIT_DIMENSIONS",
    "UNIT_ALIASES",
    "UNIT_CONVERSION_OVERRIDES",
    # Unit conversion
    "normalize_unit",
    "unit_dimension",
    "is_known_unit",
    "conversion_coefficients",
    "convert_value",
    # Calibration
    "Calibration",
    "IDENTITY_CALIBRATION",
    "effective_unit",
    "apply_calibration",
    "invert_calibration",
    # Composition
    "calibration_in_unit",
    "calibrated_value",
    "supported_units_for",
]
