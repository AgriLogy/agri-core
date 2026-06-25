"""Tests for the framework-agnostic alert evaluator.

DB-integration tests (`latest_value_for`, `recent_triggers_for_user`,
`dispatch_alerts_for_reading`, the `suggest_alert` adapter that hits the
ORM) stay in agri-api/back/analytics/tests/test_alerts.py.
"""

from __future__ import annotations

import pytest

from agri.core.alerts import (
    EQUAL_TO,
    EQUALITY_TOLERANCE,
    GREATER_THAN,
    LESS_THAN,
    SENSOR_KEY_REGISTRY,
    AlertSpec,
    evaluate,
    evaluate_alert,
    suggested_alert_payload,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestSensorKeyRegistry:
    def test_every_entry_has_required_fields(self):
        for key, spec in SENSOR_KEY_REGISTRY.items():
            assert "model" in spec, f"{key} missing 'model'"
            assert "unit" in spec, f"{key} missing 'unit'"
            assert "label" in spec, f"{key} missing 'label'"

    def test_keys_are_snake_case(self):
        for key in SENSOR_KEY_REGISTRY:
            assert key == key.lower(), key
            assert " " not in key, key

    def test_canonical_keys_present(self):
        # Smoke that the keys the front-end and Celery task explicitly
        # reference are in the registry — guards against accidental rename.
        for key in (
            "temperature_weather",
            "humidity_weather",
            "soil_moisture_medium",
            "ph_soil",
            "et0",
        ):
            assert key in SENSOR_KEY_REGISTRY


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_none_value_never_fires(self):
        assert evaluate(GREATER_THAN, 30.0, None) is False
        assert evaluate(LESS_THAN, 30.0, None) is False
        assert evaluate(EQUAL_TO, 30.0, None) is False

    def test_greater_than(self):
        assert evaluate(GREATER_THAN, 30.0, 32.0) is True
        assert evaluate(GREATER_THAN, 30.0, 30.0) is False
        assert evaluate(GREATER_THAN, 30.0, 28.0) is False

    def test_less_than(self):
        assert evaluate(LESS_THAN, 30.0, 28.0) is True
        assert evaluate(LESS_THAN, 30.0, 30.0) is False
        assert evaluate(LESS_THAN, 30.0, 32.0) is False

    def test_equal_to_within_tolerance(self):
        assert evaluate(EQUAL_TO, 30.0, 30.0) is True
        assert evaluate(EQUAL_TO, 30.0, 30.0 + EQUALITY_TOLERANCE / 2) is True
        assert evaluate(EQUAL_TO, 30.0, 30.0 + EQUALITY_TOLERANCE * 10) is False

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown alert condition"):
            evaluate("!=", 30.0, 5.0)


# ---------------------------------------------------------------------------
# AlertSpec + evaluate_alert
# ---------------------------------------------------------------------------


class TestEvaluateAlert:
    def test_routes_to_evaluate(self):
        spec = AlertSpec(condition=GREATER_THAN, threshold=30.0)
        assert evaluate_alert(spec, 32.0) is True
        assert evaluate_alert(spec, 28.0) is False

    def test_none_safe(self):
        spec = AlertSpec(condition=LESS_THAN, threshold=20.0)
        assert evaluate_alert(spec, None) is False


# ---------------------------------------------------------------------------
# suggested_alert_payload
# ---------------------------------------------------------------------------


class TestSuggestedAlertPayload:
    def test_unknown_key_returns_none(self):
        assert suggested_alert_payload("nope", [1.0, 2.0]) is None

    def test_mean_threshold_with_temperature(self):
        payload = suggested_alert_payload("temperature_weather", [20.0, 22.0, 24.0])
        assert payload is not None
        assert payload["sensor_key"] == "temperature_weather"
        assert payload["condition"] == GREATER_THAN
        assert payload["condition_nbr"] == pytest.approx(22.0)
        assert payload["mean"] == pytest.approx(22.0)
        assert payload["sample_size"] == 3
        assert payload["is_active"] is True
        assert "dernières" in payload["description"]

    def test_soil_moisture_uses_less_than(self):
        payload = suggested_alert_payload("soil_moisture_medium", [25.0, 25.0])
        assert payload is not None
        assert payload["condition"] == LESS_THAN

    def test_no_recent_values_yields_manual_threshold_message(self):
        payload = suggested_alert_payload("temperature_weather", [])
        assert payload is not None
        assert payload["mean"] is None
        assert payload["sample_size"] == 0
        assert payload["condition_nbr"] == 0.0
        assert "ajustez" in payload["description"].lower()

    def test_label_and_unit_come_from_registry(self):
        payload = suggested_alert_payload("humidity_weather", [50.0, 55.0])
        spec = SENSOR_KEY_REGISTRY["humidity_weather"]
        assert payload is not None
        assert payload["label"] == spec["label"]
        assert payload["unit"] == spec["unit"]

    def test_default_strategy_is_mean(self):
        payload = suggested_alert_payload("temperature_weather", [20.0, 22.0, 24.0])
        assert payload is not None
        assert payload["strategy"] == "mean"

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            suggested_alert_payload("temperature_weather", [1.0, 2.0], strategy="nope")

    def test_percentile_strategy_above_for_greater_than(self):
        # p90 of [10,20,30,40,50] (linear interp) = 46.0; biased above the mean (30).
        payload = suggested_alert_payload(
            "temperature_weather", [10.0, 20.0, 30.0, 40.0, 50.0], strategy="percentile"
        )
        assert payload is not None
        assert payload["condition"] == GREATER_THAN
        assert payload["strategy"] == "percentile"
        assert payload["condition_nbr"] == pytest.approx(46.0)
        assert payload["mean"] == pytest.approx(30.0)
        assert "centile" in payload["description"]

    def test_percentile_strategy_below_for_less_than(self):
        # soil_moisture → LESS_THAN → p10 of [10,20,30,40,50] = 14.0 (below mean).
        payload = suggested_alert_payload(
            "soil_moisture_medium", [10.0, 20.0, 30.0, 40.0, 50.0], strategy="percentile"
        )
        assert payload is not None
        assert payload["condition"] == LESS_THAN
        assert payload["condition_nbr"] == pytest.approx(14.0)

    def test_sd_strategy_above_for_greater_than(self):
        # mean 30 + 2·pstdev(√200≈14.142) ≈ 58.28
        payload = suggested_alert_payload(
            "temperature_weather", [10.0, 20.0, 30.0, 40.0, 50.0], strategy="sd"
        )
        assert payload is not None
        assert payload["condition"] == GREATER_THAN
        assert payload["condition_nbr"] == pytest.approx(58.28, abs=0.01)
        assert "écarts-types" in payload["description"]

    def test_sd_strategy_below_for_less_than(self):
        # soil_moisture → LESS_THAN → mean 30 − 2σ ≈ 1.72
        payload = suggested_alert_payload(
            "soil_moisture_medium", [10.0, 20.0, 30.0, 40.0, 50.0], strategy="sd"
        )
        assert payload is not None
        assert payload["condition"] == LESS_THAN
        assert payload["condition_nbr"] == pytest.approx(1.72, abs=0.01)

    def test_single_sample_edge_cases(self):
        # One reading: percentile and sd both collapse to that value.
        for strat in ("percentile", "sd"):
            payload = suggested_alert_payload("temperature_weather", [42.0], strategy=strat)
            assert payload is not None
            assert payload["condition_nbr"] == pytest.approx(42.0)

    def test_empty_values_zero_threshold_regardless_of_strategy(self):
        payload = suggested_alert_payload("temperature_weather", [], strategy="percentile")
        assert payload is not None
        assert payload["condition_nbr"] == 0.0
