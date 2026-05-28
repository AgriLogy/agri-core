"""Tests for compose_notification_email.

The Django-side gating (`should_notify`) and the integration test that
mocks `field_snapshot` stay in agri-api/back/analytics/tests/test_notification_helper.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from agri.core.agronomy import FieldInputs, SensorAggregates, ZoneParams, field_snapshot
from agri.core.notifications import compose_notification_email


def _snapshot_with_zone() -> dict:
    return field_snapshot(FieldInputs(
        date_today=date(2026, 5, 28),
        zone=ZoneParams(
            name="zone de marichage 1",
            area_m2=1000.0,
            raw_mm=40.0,
            taw_mm=80.0,
            pomp_flow_rate_l_per_s=1.0,
            irrigation_water_quantity_l=10_000.0,
            critical_moisture_pct=18.0,
        ),
        sensors=SensorAggregates(
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
        ),
        dr_today_mm=10.0,
    ))


def _snapshot_no_zone() -> dict:
    return field_snapshot(FieldInputs(
        date_today=date(2026, 5, 28), zone=None, sensors=None,
    ))


class TestComposeNotificationEmail:
    def test_full_snapshot_renders_with_expected_sections(self):
        body = compose_notification_email("Alice", _snapshot_with_zone())
        # Salutation + zone name + date
        assert body.startswith("Bonjour Alice,")
        assert "zone de marichage 1" in body
        assert "28/05/2026" in body
        # Each section header
        assert "Prévisions / météo" in body
        assert "Dernière irrigation enregistrée" in body
        assert "État actuel du sol" in body
        assert "Recommandation pour aujourd'hui" in body
        # NPK formatted as N/P/K mg/kg
        assert "120/40/180 mg/kg" in body
        # Irrigation decision text from the snapshot is interpolated verbatim
        assert "Pas d'irrigation requise" in body

    def test_missing_values_render_em_dash(self):
        body = compose_notification_email("Alice", _snapshot_no_zone())
        # Without a zone, sensors are None — many fields collapse to em-dash
        assert "—" in body
        # Default fallback zone label
        assert "votre zone" in body
        # The "no zone" irrigation message comes from agri.core.agronomy
        assert "Aucune zone configurée" in body

    def test_user_name_is_interpolated_verbatim(self):
        body = compose_notification_email("Zakaria", _snapshot_with_zone())
        assert "Bonjour Zakaria," in body

    def test_zone_label_falls_back_when_name_is_none(self):
        snap = _snapshot_with_zone()
        snap["zone_name"] = None
        body = compose_notification_email("Alice", snap)
        assert "votre zone" in body
