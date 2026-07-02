"""Tests for the FAO-56 hourly math + the compute_zone_et0 handler.

Mirrors the analogous tests in agri-api/back/analytics/tests/test_agronomy.py
that exercise the math directly (the integration tests against the Django
ORM stay in agri-api).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agri.core.agronomy import (
    ALBEDO_SHORT_CROP,
    CLOUD_RATIO_MAX,
    CLOUD_RATIO_MIN,
    DEPLOYMENT_LOCAL_TZ,
    SIGMA,
    Et0Inputs,
    ZoneEt0,
    actual_vapor_pressure_kpa,
    cloudiness_ratio,
    compute_zone_et0,
    equation_of_time_minutes,
    extraterrestrial_radiation_hourly_mjm2h,
    is_daytime,
    penman_monteith_hourly_mm,
    psychrometric_constant_kpa_per_c,
    saturation_vapor_pressure_kpa,
    slope_svp_kpa_per_c,
    solar_time_correction_hours,
    vpd_kpa,
    wind_speed_at_2m,
    wperm2_to_mjm2_per_hour,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_sigma_is_hourly_not_daily(self):
        # The legacy bug used 4.903e-9 (daily); the hourly value is 2.043e-10.
        assert pytest.approx(2.043e-10) == SIGMA
        assert SIGMA < 4.903e-9 / 20  # > 20× smaller than the daily value

    def test_albedo_is_grass_reference(self):
        assert ALBEDO_SHORT_CROP == 0.23


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


class TestSaturationVaporPressure:
    def test_known_temps(self):
        # FAO-56 reference values
        assert saturation_vapor_pressure_kpa(0.0) == pytest.approx(0.6108, rel=1e-3)
        assert saturation_vapor_pressure_kpa(20.0) == pytest.approx(2.339, rel=1e-2)
        assert saturation_vapor_pressure_kpa(40.0) == pytest.approx(7.384, rel=1e-1)


class TestActualVaporPressure:
    def test_clamps_rh_to_one_to_hundred(self):
        es = saturation_vapor_pressure_kpa(20.0)
        # Below 1% gets clamped to 1%
        assert actual_vapor_pressure_kpa(20.0, 0.0) == pytest.approx(es * 0.01)
        assert actual_vapor_pressure_kpa(20.0, -50.0) == pytest.approx(es * 0.01)
        # Above 100% gets clamped to 100%
        assert actual_vapor_pressure_kpa(20.0, 100.0) == pytest.approx(es)
        assert actual_vapor_pressure_kpa(20.0, 150.0) == pytest.approx(es)


class TestSlopeSvp:
    def test_positive_at_typical_temp(self):
        # The slope is always positive; check a typical value
        assert slope_svp_kpa_per_c(20.0) > 0


class TestPsychrometric:
    def test_proportional_to_pressure(self):
        # γ ≈ 0.000665 · P
        assert psychrometric_constant_kpa_per_c(101.3) == pytest.approx(0.0673, rel=1e-3)


class TestUnitConversions:
    def test_wperm2_to_mjm2_per_hour(self):
        # 1000 W/m² for 1 hour = 3.6 MJ/m²
        assert wperm2_to_mjm2_per_hour(1000.0) == pytest.approx(3.6)


class TestWindAt2m:
    def test_identity_at_2m(self):
        assert wind_speed_at_2m(5.0) == 5.0

    def test_projects_down_from_10m(self):
        # FAO-56 eq. 47: u2 = uz * 4.87 / ln(67.8 * z - 5.42)
        u2 = wind_speed_at_2m(5.0, sensor_height_m=10.0)
        assert 3.0 < u2 < 4.0


class TestVpd:
    def test_non_negative(self):
        # VPD is always >= 0
        assert vpd_kpa(20.0, 100.0) == 0.0
        assert vpd_kpa(20.0, 50.0) > 0
        assert vpd_kpa(40.0, 10.0) > vpd_kpa(20.0, 50.0)


class TestSolarGeometry:
    def test_equation_of_time(self):
        # EoT range is roughly -14 to +16 minutes over the year
        eot_jan = equation_of_time_minutes(15)
        eot_jun = equation_of_time_minutes(170)
        assert -20 < eot_jan < 20
        assert -20 < eot_jun < 20

    def test_solar_time_correction_morocco_at_lst(self):
        # At Lst itself (15° E), lon term is zero; only EoT contributes.
        correction = solar_time_correction_hours(80, lon_deg=15.0)
        # ~minutes / 60 — small
        assert abs(correction) < 0.5

    def test_extraterrestrial_radiation_nonzero_at_noon(self):
        # Mid-day in summer at a temperate latitude → Ra > 2 MJ/m²/h
        ra = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=33.0,
            lon_deg=-7.6,
            day_of_year=170,
            local_clock_hour=12.0,
        )
        assert ra > 2.0

    def test_extraterrestrial_radiation_zero_at_midnight(self):
        ra = extraterrestrial_radiation_hourly_mjm2h(
            lat_deg=33.0,
            lon_deg=-7.6,
            day_of_year=170,
            local_clock_hour=0.0,
        )
        assert ra == 0.0


class TestCloudinessRatio:
    def test_clamps_to_min(self):
        assert cloudiness_ratio(0.0, 10.0) == CLOUD_RATIO_MIN

    def test_clamps_to_max(self):
        # Rs > Rso (sensor noise) gets clamped to 1.0
        assert cloudiness_ratio(20.0, 10.0) == CLOUD_RATIO_MAX

    def test_uses_min_when_rso_zero(self):
        assert cloudiness_ratio(5.0, 0.0) == CLOUD_RATIO_MIN

    def test_unclamped_in_range(self):
        assert cloudiness_ratio(5.0, 10.0) == pytest.approx(0.5)


class TestIsDaytime:
    def test_daytime_when_rs_positive(self):
        assert is_daytime(0.5) is True

    def test_not_daytime_at_zero(self):
        assert is_daytime(0.0) is False


# ---------------------------------------------------------------------------
# penman_monteith_hourly_mm — composite math
# ---------------------------------------------------------------------------


class TestPenmanMonteithHourly:
    NOON_INPUTS = dict(
        temp_c=25.0,
        rh_pct=50.0,
        wind_ms=2.0,
        pressure_kpa=101.3,
        rs_mjm2h=2.5,
    )

    def test_noon_summer_produces_positive_et0(self):
        result = penman_monteith_hourly_mm(**self.NOON_INPUTS)
        assert result["et0_mm_per_h"] > 0
        assert result["daytime"] is True

    def test_night_low_et0(self):
        night = {**self.NOON_INPUTS, "rs_mjm2h": 0.0}
        result = penman_monteith_hourly_mm(**night)
        # At night Rs=0 → Rn negative → et0 floors to 0
        assert result["et0_mm_per_h"] >= 0
        assert result["daytime"] is False

    def test_higher_temp_pushes_et0_up_at_same_rh(self):
        cool = penman_monteith_hourly_mm(**{**self.NOON_INPUTS, "temp_c": 15.0})
        warm = penman_monteith_hourly_mm(**{**self.NOON_INPUTS, "temp_c": 35.0})
        assert warm["et0_mm_per_h"] > cool["et0_mm_per_h"]

    def test_with_ra_uses_proper_cloudiness(self):
        # Identical inputs except ra; check the result differs.
        no_ra = penman_monteith_hourly_mm(**self.NOON_INPUTS)
        with_ra = penman_monteith_hourly_mm(**self.NOON_INPUTS, ra_mjm2h=3.5)
        assert with_ra["rn_mjm2h"] != no_ra["rn_mjm2h"]


# ---------------------------------------------------------------------------
# compute_zone_et0 handler
# ---------------------------------------------------------------------------


def _full_inputs(**overrides) -> Et0Inputs:
    base = dict(
        zone_id=42,
        user_id=7,
        timestamp=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        temp_c=25.0,
        rh_pct=50.0,
        wind_ms=2.0,
        rs_wm2=700.0,  # ~2.5 MJ/m²/h
        pressure_hpa=1013.0,
        latitude=33.0,
        longitude=-7.6,
    )
    base.update(overrides)
    return Et0Inputs(**base)


class TestComputeZoneEt0:
    def test_returns_zone_et0_with_correct_identity(self):
        result = compute_zone_et0(_full_inputs())
        assert isinstance(result, ZoneEt0)
        assert result.zone_id == 42
        assert result.user_id == 7
        assert result.et0_mm_per_h > 0
        assert result.vpd_kpa >= 0

    def test_returns_none_if_temp_missing(self):
        assert compute_zone_et0(_full_inputs(temp_c=None)) is None

    def test_returns_none_if_pressure_missing(self):
        assert compute_zone_et0(_full_inputs(pressure_hpa=None)) is None

    def test_works_without_lat_lon(self):
        # No lat/lon → falls back to 0.75 mid-band heuristic; should still
        # produce a result.
        result = compute_zone_et0(_full_inputs(latitude=None, longitude=None))
        assert result is not None
        assert result.et0_mm_per_h >= 0

    def test_timestamp_carried_through(self):
        ts = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
        result = compute_zone_et0(_full_inputs(timestamp=ts))
        assert result is not None
        assert result.timestamp == ts

    def test_as_dict_round_trip(self):
        result = compute_zone_et0(_full_inputs())
        d = result.as_dict()
        assert d["zone_id"] == 42
        assert d["et0_mm_per_h"] == result.et0_mm_per_h
        assert d["timestamp"] == result.timestamp.isoformat()

    def test_local_tz_is_casablanca(self):
        # Sanity that our deployment tz constant matches the test expectation.
        assert str(DEPLOYMENT_LOCAL_TZ) == "Africa/Casablanca"
