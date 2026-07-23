"""Tests for the sensor-calibration + unit-conversion logic (CAL-1 core).

Pure math only — no database, no framework. Three kinds of assertion:

* **known values** hand-computed from physics (25 °C = 77 °F, 1 bar = 100 kPa,
  1 mS/cm = 1 dS/m), so the table is checked against reality and not only
  against itself;
* **round-trip properties** — calibrate then invert is the identity, and a
  unit change followed by the reverse change is the identity;
* **failure modes** — an unknown unit or a cross-dimension pair raises,
  a disabled calibration returns the raw value.
"""

from __future__ import annotations

import pytest

from agri.core.alerts import SENSOR_KEY_REGISTRY
from agri.core.calibration import (
    IDENTITY_CALIBRATION,
    UNIT_ALIASES,
    UNIT_DIMENSIONS,
    Calibration,
    NonInvertibleCalibrationError,
    UnknownUnitConversionError,
    UnknownUnitError,
    apply_calibration,
    calibrated_value,
    calibration_in_unit,
    conversion_coefficients,
    convert_value,
    effective_unit,
    invert_calibration,
    is_known_unit,
    normalize_unit,
    supported_units_for,
    unit_dimension,
)

# ---------------------------------------------------------------------------
# Applying a calibration
# ---------------------------------------------------------------------------


class TestApplyCalibration:
    def test_affine_formula(self):
        cal = Calibration(scale_a=1.02, offset_b=-0.4)
        assert apply_calibration(10.0, cal) == pytest.approx(9.8)

    def test_identity_returns_raw(self):
        assert apply_calibration(12.34, IDENTITY_CALIBRATION) == 12.34
        assert IDENTITY_CALIBRATION.is_identity is True

    def test_inactive_returns_raw(self):
        cal = Calibration(scale_a=2.0, offset_b=5.0, is_active=False)
        assert apply_calibration(10.0, cal) == 10.0

    def test_no_calibration_returns_raw(self):
        assert apply_calibration(10.0, None) == 10.0

    def test_none_raw_stays_none(self):
        cal = Calibration(scale_a=2.0, offset_b=5.0)
        assert apply_calibration(None, cal) is None
        assert apply_calibration(None, None) is None

    def test_zero_raw_is_not_treated_as_missing(self):
        cal = Calibration(scale_a=2.0, offset_b=5.0)
        assert apply_calibration(0.0, cal) == pytest.approx(5.0)

    def test_offset_only(self):
        assert apply_calibration(20.0, Calibration(offset_b=-1.5)) == pytest.approx(18.5)

    def test_scale_only(self):
        assert apply_calibration(20.0, Calibration(scale_a=0.5)) == pytest.approx(10.0)


class TestInvertCalibration:
    @pytest.mark.parametrize("raw", [-40.0, 0.0, 0.7, 21.5, 1234.567])
    @pytest.mark.parametrize(
        ("scale_a", "offset_b"),
        [(1.0, 0.0), (1.02, -0.4), (0.5, 12.0), (-3.25, 7.5), (1e-3, 1e4)],
    )
    def test_round_trip_returns_original(self, raw, scale_a, offset_b):
        cal = Calibration(scale_a=scale_a, offset_b=offset_b)
        assert invert_calibration(apply_calibration(raw, cal), cal) == pytest.approx(raw)

    def test_inactive_is_a_no_op_both_ways(self):
        cal = Calibration(scale_a=2.0, offset_b=5.0, is_active=False)
        assert invert_calibration(10.0, cal) == 10.0

    def test_none_stays_none(self):
        assert invert_calibration(None, Calibration(scale_a=2.0)) is None

    def test_zero_scale_is_not_invertible(self):
        with pytest.raises(NonInvertibleCalibrationError):
            invert_calibration(5.0, Calibration(scale_a=0.0, offset_b=5.0))


class TestCalibrationFromRow:
    class _Row:
        scale_a = 1.5
        offset_b = -2.0
        unit = " uS/cm "
        is_active = False

    def test_duck_typed_row(self):
        cal = Calibration.from_row(self._Row())
        assert (cal.scale_a, cal.offset_b, cal.unit, cal.is_active) == (
            1.5,
            -2.0,
            "μS/cm",
            False,
        )

    def test_missing_attributes_fall_back_to_column_defaults(self):
        cal = Calibration.from_row(object())
        assert cal == IDENTITY_CALIBRATION


# ---------------------------------------------------------------------------
# Unit table — known values, hand-computed from physics
# ---------------------------------------------------------------------------


class TestKnownConversions:
    @pytest.mark.parametrize(
        ("value", "from_unit", "to_unit", "expected"),
        [
            # Temperature
            (25.0, "°C", "°F", 77.0),
            (-40.0, "°C", "°F", -40.0),
            (0.0, "°C", "K", 273.15),
            (212.0, "°F", "°C", 100.0),
            (300.0, "K", "°C", 26.85),
            # Pressure
            (1.0, "bar", "kPa", 100.0),
            (1013.25, "hPa", "kPa", 101.325),
            (1.0, "atm", "hPa", 1013.25),
            (1.0, "bar", "psi", 14.503773773),
            (101.325, "kPa", "atm", 1.0),
            # Conductivity
            (1.0, "mS/cm", "dS/m", 1.0),
            (1500.0, "μS/cm", "mS/cm", 1.5),
            (1.0, "dS/m", "μS/cm", 1000.0),
            (0.1, "S/m", "mS/cm", 1.0),
            # Length
            (25.4, "mm", "in", 1.0),
            (1.0, "m", "mm", 1000.0),
            (1.0, "in", "cm", 2.54),
            # Speed
            (1.0, "m/s", "km/h", 3.6),
            (100.0, "km/h", "mph", 62.1371192),
            (1.0, "mph", "m/s", 0.44704),
            # Depth rate
            (24.0, "mm/day", "mm/h", 1.0),
            (1.0, "in/h", "mm/h", 25.4),
            # Volumetric flow
            (1.0, "m³/h", "L/h", 1000.0),
            (1.0, "L/s", "m³/h", 3.6),
            # Concentration
            (1.0, "g/L", "mg/L", 1000.0),
            (500.0, "mg/L", "ppm", 500.0),
            # Irradiance / energy
            (1.0, "kW/m²", "W/m²", 1000.0),
            (3.6, "MJ/m²/h", "W/m²", 1000.0),
            (1.0, "kWh", "MJ", 3.6),
            (2500.0, "Wh", "kWh", 2.5),
            # Voltage
            (3700.0, "mV", "V", 3.7),
        ],
    )
    def test_known_value(self, value, from_unit, to_unit, expected):
        assert convert_value(value, from_unit, to_unit) == pytest.approx(expected)

    def test_none_value_stays_none(self):
        assert convert_value(None, "°C", "°F") is None

    def test_same_unit_is_identity_even_when_unknown(self):
        assert conversion_coefficients("furlong", "furlong") == (1.0, 0.0)
        assert convert_value(3.0, "widgets", "widgets") == 3.0


class TestUnitNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" uS/cm ", "μS/cm"),
            ("µS/cm", "μS/cm"),  # MICRO SIGN → GREEK MU
            ("degC", "°C"),
            ("kph", "km/h"),
            ("m3/h", "m³/h"),
            ("W/m2", "W/m²"),
            ("percent", "%"),
            (None, ""),
            ("   ", ""),
        ],
    )
    def test_aliases(self, raw, expected):
        assert normalize_unit(raw) == expected

    def test_alias_is_convertible(self):
        assert convert_value(1000.0, "uS/cm", "dS/m") == pytest.approx(1.0)

    def test_dimension_lookup(self):
        assert unit_dimension("hPa") == "pressure"
        assert unit_dimension("nonsense") is None
        assert is_known_unit("°F") is True
        assert is_known_unit("nonsense") is False


class TestUnknownConversionsRaiseLoudly:
    def test_unknown_source_unit(self):
        with pytest.raises(UnknownUnitError):
            conversion_coefficients("smoots", "mm")

    def test_unknown_target_unit(self):
        with pytest.raises(UnknownUnitError):
            conversion_coefficients("mm", "smoots")

    def test_cross_dimension_pair(self):
        with pytest.raises(UnknownUnitConversionError):
            conversion_coefficients("°C", "kPa")

    def test_single_unit_dimension_cannot_leave_itself(self):
        with pytest.raises(UnknownUnitConversionError):
            conversion_coefficients("pH", "%")

    def test_dbm_has_no_affine_partner(self):
        # dBm → mW is logarithmic; deliberately absent rather than approximated.
        with pytest.raises(UnknownUnitConversionError):
            conversion_coefficients("dBm", "V")

    def test_convert_value_propagates_the_error(self):
        with pytest.raises(UnknownUnitError):
            convert_value(1.0, "mm", "smoots")


class TestUnitTableRoundTrips:
    @pytest.mark.parametrize("dimension", sorted(UNIT_DIMENSIONS))
    def test_every_pair_round_trips(self, dimension):
        units = sorted(UNIT_DIMENSIONS[dimension].units)
        for source in units:
            for target in units:
                there = convert_value(37.5, source, target)
                back = convert_value(there, target, source)
                assert back == pytest.approx(37.5), f"{source} -> {target} -> {source}"

    @pytest.mark.parametrize("dimension", sorted(UNIT_DIMENSIONS))
    def test_base_unit_is_declared_and_is_the_identity(self, dimension):
        spec = UNIT_DIMENSIONS[dimension]
        assert spec.base in spec.units
        assert spec.units[spec.base].to_base_scale == 1.0
        assert spec.units[spec.base].to_base_offset == 0.0

    def test_units_are_not_shared_between_dimensions(self):
        seen: set[str] = set()
        for spec in UNIT_DIMENSIONS.values():
            clash = seen & set(spec.units)
            assert not clash, f"unit declared in two dimensions: {clash}"
            seen |= set(spec.units)

    def test_aliases_all_resolve_to_a_declared_unit(self):
        for alias, canonical in UNIT_ALIASES.items():
            assert is_known_unit(canonical), f"alias {alias!r} → unknown unit {canonical!r}"


# ---------------------------------------------------------------------------
# Composition: calibration ∘ unit conversion
# ---------------------------------------------------------------------------


class TestCalibrationInUnit:
    def test_coefficients_recomputed_for_celsius_to_fahrenheit(self):
        # real_°C = raw * 1.02 - 0.4  →  real_°F = raw * 1.836 + 31.28
        # a' = 1.02 * 1.8 = 1.836 ; b' = -0.4 * 1.8 + 32 = 31.28
        cal = Calibration(scale_a=1.02, offset_b=-0.4, unit="°C")
        converted = calibration_in_unit(cal, "°F")
        assert converted.scale_a == pytest.approx(1.836)
        assert converted.offset_b == pytest.approx(31.28)
        assert converted.unit == "°F"

    def test_pure_scale_conversion_leaves_offset_scaled_only(self):
        # bar → kPa has no offset, so b just scales by 100.
        cal = Calibration(scale_a=1.05, offset_b=0.02, unit="bar")
        converted = calibration_in_unit(cal, "kPa")
        assert converted.scale_a == pytest.approx(105.0)
        assert converted.offset_b == pytest.approx(2.0)

    def test_agrees_with_convert_after_apply(self):
        cal = Calibration(scale_a=1.02, offset_b=-0.4, unit="°C")
        converted = calibration_in_unit(cal, "°F")
        for raw in (-10.0, 0.0, 18.3, 41.7):
            direct = apply_calibration(raw, converted)
            two_step = convert_value(apply_calibration(raw, cal), "°C", "°F")
            assert direct == pytest.approx(two_step)

    def test_unit_change_and_back_is_identity(self):
        cal = Calibration(scale_a=1.02, offset_b=-0.4, unit="°C")
        there_and_back = calibration_in_unit(calibration_in_unit(cal, "°F"), "°C")
        assert there_and_back.scale_a == pytest.approx(cal.scale_a)
        assert there_and_back.offset_b == pytest.approx(cal.offset_b)
        assert there_and_back.unit == cal.unit

    @pytest.mark.parametrize(
        ("source", "target"),
        [("°C", "K"), ("kPa", "psi"), ("μS/cm", "dS/m"), ("mm", "in"), ("m/s", "mph")],
    )
    def test_round_trip_across_dimensions(self, source, target):
        cal = Calibration(scale_a=0.97, offset_b=3.5, unit=source)
        back = calibration_in_unit(calibration_in_unit(cal, target), source)
        assert back.scale_a == pytest.approx(cal.scale_a)
        assert back.offset_b == pytest.approx(cal.offset_b)

    def test_source_unit_falls_back_to_the_sensor_registry(self):
        # Blank unit = "keep the registry default", here °C for air temperature.
        cal = Calibration(scale_a=1.0, offset_b=0.5, unit="")
        converted = calibration_in_unit(cal, "°F", sensor_key="temperature_weather")
        assert converted.scale_a == pytest.approx(1.8)
        assert converted.offset_b == pytest.approx(32.9)  # 0.5 * 1.8 + 32
        assert converted.unit == "°F"

    def test_unresolvable_source_unit_raises(self):
        with pytest.raises(UnknownUnitError):
            calibration_in_unit(Calibration(scale_a=2.0), "°F")
        with pytest.raises(UnknownUnitError):
            calibration_in_unit(Calibration(scale_a=2.0), "°F", sensor_key="not_a_sensor")

    def test_unknown_target_unit_raises(self):
        with pytest.raises(UnknownUnitError):
            calibration_in_unit(Calibration(unit="°C"), "smoots")

    def test_inactive_calibration_is_still_retargeted(self):
        cal = Calibration(scale_a=1.02, offset_b=-0.4, unit="°C", is_active=False)
        converted = calibration_in_unit(cal, "°F")
        assert converted.is_active is False
        assert converted.scale_a == pytest.approx(1.836)
        # …but applying it still returns the raw value untouched.
        assert apply_calibration(20.0, converted) == 20.0


class TestEffectiveUnit:
    def test_calibration_unit_wins(self):
        cal = Calibration(unit="°F")
        assert effective_unit("temperature_weather", cal) == "°F"

    def test_blank_falls_back_to_registry(self):
        assert effective_unit("temperature_weather", Calibration()) == "°C"
        assert effective_unit("water_pressure") == "bar"

    def test_unknown_sensor_key_returns_blank(self):
        assert effective_unit("not_a_sensor") == ""

    def test_registry_unit_aliases_are_normalised(self):
        assert effective_unit("water_ec") == "μS/cm"


class TestCalibratedValue:
    def test_correction_then_conversion(self):
        cal = Calibration(scale_a=1.02, offset_b=-0.4, unit="°C")
        # raw 20 → 20.0 °C → 68.0 °F
        assert calibrated_value(20.0, cal, target_unit="°F") == pytest.approx(68.0)

    def test_matches_calibration_in_unit(self):
        cal = Calibration(scale_a=0.98, offset_b=1.2, unit="bar")
        retargeted = calibration_in_unit(cal, "kPa")
        for raw in (0.0, 1.0, 4.75):
            assert calibrated_value(raw, cal, target_unit="kPa") == pytest.approx(
                apply_calibration(raw, retargeted)
            )

    def test_no_target_unit_is_plain_calibration(self):
        cal = Calibration(scale_a=2.0, offset_b=1.0, unit="°C")
        assert calibrated_value(10.0, cal) == pytest.approx(21.0)

    def test_none_raw_stays_none(self):
        assert calibrated_value(None, Calibration(unit="°C"), target_unit="°F") is None

    def test_conversion_without_a_calibration_uses_the_registry_unit(self):
        assert calibrated_value(
            25.0, None, sensor_key="temperature_weather", target_unit="°F"
        ) == pytest.approx(77.0)

    def test_inactive_calibration_converts_the_raw_value(self):
        cal = Calibration(scale_a=5.0, offset_b=5.0, unit="°C", is_active=False)
        assert calibrated_value(25.0, cal, target_unit="°F") == pytest.approx(77.0)

    def test_unresolvable_unit_raises(self):
        with pytest.raises(UnknownUnitError):
            calibrated_value(1.0, Calibration(), target_unit="°F")


class TestSupportedUnitsFor:
    def test_temperature_sensor(self):
        assert supported_units_for("temperature_weather") == ["K", "°C", "°F"]

    def test_conductivity_sensor(self):
        assert supported_units_for("water_ec") == ["S/m", "dS/m", "mS/cm", "μS/cm"]

    def test_unknown_sensor_key_is_empty_not_an_error(self):
        assert supported_units_for("not_a_sensor") == []

    def test_every_registry_unit_is_in_the_table(self):
        missing = {
            key: spec["unit"]
            for key, spec in SENSOR_KEY_REGISTRY.items()
            if not is_known_unit(str(spec["unit"]))
        }
        assert not missing, f"SENSOR_KEY_REGISTRY units absent from the unit table: {missing}"
