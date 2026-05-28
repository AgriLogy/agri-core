"""Tests for the framework-agnostic agronomy handler.

These mirror the unit-test subset of
``agri-api/back/analytics/tests/test_agronomy.py``. The DB-integration
tests stay over in agri-api against the real ORM; the math + decision
+ snapshot-assembly logic now lives here and is testable without
Django / Postgres.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from agri.core.agronomy import (
    DEFAULT_CRITICAL_SOIL_MOISTURE_PCT,
    DEFAULT_IRRIGATION_EFFICIENCY,
    DEFAULT_KC,
    FieldInputs,
    IrrigationDecision,
    SensorAggregates,
    ZoneParams,
    cumulative_dr_after_missed_days,
    effective_rainfall_mm,
    etc_mm,
    field_snapshot,
    irrigation_decision_dr,
    update_daily_depletion,
)

# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


class TestEffectiveRainfall:
    def test_default_alpha(self):
        assert effective_rainfall_mm(10.0) == pytest.approx(8.0)

    def test_custom_alpha(self):
        assert effective_rainfall_mm(10.0, alpha=0.5) == pytest.approx(5.0)

    def test_floor_at_zero(self):
        assert effective_rainfall_mm(-3.0) == 0.0


class TestEtc:
    def test_default_kc(self):
        assert etc_mm(5.0) == pytest.approx(5.0)

    def test_kc_below_one(self):
        assert etc_mm(5.0, kc=0.6) == pytest.approx(3.0)

    def test_adds_permeability_loss(self):
        assert etc_mm(5.0, kc=0.8, permeability_loss_mm=1.0) == pytest.approx(5.0)


class TestUpdateDailyDepletion:
    def test_typical_day(self):
        # Dr,yesterday = 5, ETc = 4, Pe = 1, In = 0  → Dr,today = 8
        d = update_daily_depletion(
            dr_yesterday_mm=5.0, etc_today_mm=4.0, pe_today_mm=1.0,
            irrigation_applied_mm=0.0,
        )
        assert d == pytest.approx(8.0)

    def test_irrigation_brings_dr_below_zero_clamped(self):
        d = update_daily_depletion(
            dr_yesterday_mm=5.0, etc_today_mm=2.0, pe_today_mm=0.0,
            irrigation_applied_mm=20.0,
        )
        assert d == 0.0


class TestCumulativeDr:
    def test_no_missed_days(self):
        d = cumulative_dr_after_missed_days(
            dr_baseline_mm=10.0,
            et0_per_day_mm=[],
            rain_per_day_mm=[],
        )
        assert d == 10.0

    def test_missed_days_accumulate_etc(self):
        d = cumulative_dr_after_missed_days(
            dr_baseline_mm=0.0,
            et0_per_day_mm=[4.0, 4.0, 4.0],
            rain_per_day_mm=[0.0, 0.0, 0.0],
            kc=1.0,
        )
        # 3 days × 4 mm ETc each, no rain
        assert d == pytest.approx(12.0)

    def test_rain_offsets_etc(self):
        d = cumulative_dr_after_missed_days(
            dr_baseline_mm=0.0,
            et0_per_day_mm=[4.0, 4.0],
            rain_per_day_mm=[10.0, 0.0],
            kc=1.0,
            alpha=0.8,
        )
        # ETc = 8, Pe = 8 (10×0.8), so effective = 0
        assert d == 0.0


# ---------------------------------------------------------------------------
# irrigation_decision_dr (one test per branch)
# ---------------------------------------------------------------------------


class TestIrrigationDecisionDr:
    DEFAULT_ARGS = dict(
        raw_mm=40.0,
        soil_moisture_pct=30.0,
        critical_moisture_pct=20.0,
        zone_area_m2=1000.0,
        flow_rate_m3h=5.0,
        max_water_per_day_m3=0.0,
    )

    def test_no_stress_suspends(self):
        d = irrigation_decision_dr(dr_today_mm=10.0, **self.DEFAULT_ARGS)
        assert d.irrigate is False
        assert d.reason == "no_stress"
        assert d.net_mm == 0.0

    def test_stress_irrigates_full_dr(self):
        d = irrigation_decision_dr(dr_today_mm=45.0, **self.DEFAULT_ARGS)
        assert d.irrigate is True
        assert d.reason == "stress"
        assert d.net_mm == 45.0

    def test_soil_moisture_low_irrigates_even_below_raw(self):
        args = dict(self.DEFAULT_ARGS, soil_moisture_pct=15.0)
        d = irrigation_decision_dr(dr_today_mm=5.0, **args)
        assert d.irrigate is True
        assert d.reason == "soil_moisture_low"

    def test_rain_will_suffice_suspends_when_dr_below_raw(self):
        d = irrigation_decision_dr(
            dr_today_mm=10.0, precipitation_forecast_mm=10.0, **self.DEFAULT_ARGS,
        )
        assert d.irrigate is False
        assert d.reason == "rain_will_suffice"

    def test_complementary_uses_dr_minus_pe(self):
        d = irrigation_decision_dr(
            dr_today_mm=45.0, precipitation_forecast_mm=10.0, **self.DEFAULT_ARGS,
        )
        assert d.irrigate is True
        assert d.reason == "complementary"
        # Pe = 0.8 * 10 = 8 → net = 45 - 8 = 37
        assert d.net_mm == pytest.approx(37.0)

    def test_volume_caps_at_max_water_per_day(self):
        args = dict(self.DEFAULT_ARGS, max_water_per_day_m3=5.0)
        d = irrigation_decision_dr(dr_today_mm=200.0, **args)
        assert d.capped_to_daily_max is True
        assert d.volume_m3 == 5.0

    def test_long_duration_triggers_split(self):
        # 1 ha zone, 1 m³/h flow → very long duration → split kicks in
        args = dict(self.DEFAULT_ARGS, zone_area_m2=10000.0, flow_rate_m3h=1.0)
        d = irrigation_decision_dr(dr_today_mm=45.0, **args)
        assert d.morning_volume_m3 is not None
        assert d.evening_volume_m3 is not None
        assert math.isclose(
            d.morning_volume_m3 + d.evening_volume_m3, d.volume_m3
        )

    def test_gross_uses_efficiency(self):
        d = irrigation_decision_dr(dr_today_mm=45.0, **self.DEFAULT_ARGS)
        # gross = net * kr / efficiency = 45 * 1 / 0.85
        assert d.gross_mm == pytest.approx(45.0 / DEFAULT_IRRIGATION_EFFICIENCY)


# ---------------------------------------------------------------------------
# field_snapshot — the assembly + branching logic
# ---------------------------------------------------------------------------


def _today() -> date:
    return date(2026, 5, 28)


def _full_zone() -> ZoneParams:
    return ZoneParams(
        name="zone de marichage 1",
        area_m2=1000.0,
        raw_mm=40.0,
        taw_mm=80.0,
        pomp_flow_rate_l_per_s=1.0,           # 3.6 m³/h
        irrigation_water_quantity_l=10_000.0, # 10 m³/day cap
        critical_moisture_pct=18.0,
    )


def _full_sensors() -> SensorAggregates:
    return SensorAggregates(
        yesterday_temp_c=22.0,
        today_temp_c=25.0,
        yesterday_humidity_pct=55.0,
        today_humidity_pct=60.0,
        et0_today_mm=4.2,
        soil_moisture_pct=30.0,
        soil_temperature_c=21.0,
        soil_ph=6.8,
        soil_ec=820.0,
        soil_salinity=410.0,
        npk_n=120.0,
        npk_p=40.0,
        npk_k=180.0,
        last_irrigation_at=datetime(2026, 5, 27, 6, 30, tzinfo=UTC),
        last_irrigation_l=500.0,
    )


class TestFieldSnapshot:
    def test_no_zone_returns_empty_shape(self):
        snap = field_snapshot(FieldInputs(
            date_today=_today(), zone=None, sensors=None,
        ))
        assert snap["zone_name"] is None
        assert snap["irrigation_decision"].startswith("Aucune zone")
        assert snap["et0_today_mm"] is None
        assert snap["kc_used"] == DEFAULT_KC
        assert snap["dr_today_mm"] is None
        assert snap["decision_reason"] is None

    def test_full_data_without_dr_falls_back_to_legacy_branch(self):
        snap = field_snapshot(FieldInputs(
            date_today=_today(), zone=_full_zone(), sensors=_full_sensors(),
        ))
        assert snap["zone_name"] == "zone de marichage 1"
        assert snap["et0_today_mm"] == pytest.approx(4.2)
        # No dr_today_mm provided → decision is the legacy message
        assert snap["decision_reason"] is None
        # Soil moisture (30%) is above the legacy default critical threshold (20%)
        assert "Pas d'irrigation requise" in snap["irrigation_decision"]

    def test_with_dr_takes_the_dr_branch(self):
        snap = field_snapshot(FieldInputs(
            date_today=_today(),
            zone=_full_zone(),
            sensors=_full_sensors(),
            dr_today_mm=45.0,  # > raw_mm (40) → stress
        ))
        assert snap["decision_reason"] == "stress"
        assert snap["recommended_volume_m3"] is not None
        assert snap["recommended_volume_m3"] > 0
        assert snap["dr_today_mm"] == 45.0
        assert snap["raw_mm"] == 40.0

    def test_no_stress_with_dr_does_not_recommend(self):
        snap = field_snapshot(FieldInputs(
            date_today=_today(),
            zone=_full_zone(),
            sensors=_full_sensors(),
            dr_today_mm=10.0,  # < raw_mm (40), soil_moisture 30 > critical 18
        ))
        assert snap["decision_reason"] == "no_stress"
        assert snap["recommended_volume_m3"] == 0.0

    def test_low_moisture_without_dr_uses_legacy_critical_threshold(self):
        sensors = _full_sensors()
        sensors.soil_moisture_pct = 10.0  # below the 20% legacy default
        snap = field_snapshot(FieldInputs(
            date_today=_today(), zone=_full_zone(), sensors=sensors,
        ))
        assert "Irrigation recommandée" in snap["irrigation_decision"]
        assert f"{int(DEFAULT_CRITICAL_SOIL_MOISTURE_PCT)} %" in snap[
            "irrigation_decision"
        ]

    def test_rain_forecast_suspends_when_no_stress(self):
        snap = field_snapshot(FieldInputs(
            date_today=_today(),
            zone=_full_zone(),
            sensors=_full_sensors(),
            dr_today_mm=10.0,
            precipitation_forecast_mm=10.0,
        ))
        assert snap["decision_reason"] == "rain_will_suffice"

    def test_keys_match_email_template_contract(self):
        # The notification email reads these by name; if a key drops out the
        # template formats a KeyError at send time. Lock them in here.
        snap = field_snapshot(FieldInputs(
            date_today=_today(), zone=_full_zone(), sensors=_full_sensors(),
        ))
        expected = {
            "zone_name", "date_today", "yesterday_temp_c", "today_temp_c",
            "yesterday_humidity_pct", "today_humidity_pct", "et0_today_mm",
            "soil_moisture_pct", "soil_temperature_c", "soil_ph", "soil_ec",
            "soil_salinity", "npk_n", "npk_p", "npk_k",
            "last_irrigation_at", "last_irrigation_l",
            "perfect_irrigation_window", "kc_used", "irrigation_decision",
            "dr_today_mm", "raw_mm", "taw_mm", "decision_reason",
            "recommended_volume_m3", "recommended_duration_min",
            "morning_volume_m3", "evening_volume_m3",
        }
        assert set(snap.keys()) == expected


class TestIrrigationDecisionDataclass:
    def test_is_a_dataclass(self):
        d = IrrigationDecision(
            irrigate=False, reason="no_stress", net_mm=0.0, gross_mm=0.0,
            volume_m3=0.0, duration_hr=0.0,
            morning_volume_m3=None, evening_volume_m3=None,
            capped_to_daily_max=False,
        )
        assert d.irrigate is False
