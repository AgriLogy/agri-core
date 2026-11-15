"""Tests for agri.core.water_level.

The basin figures are the worked example of the developer brief (agrilogy-front
#261): an 8 x 3 m basin, 4.80 m max height, 10 m³ minimum reserve, level
3.62 m → 86.88 m³, 75.4 %, 28.32 m³ free, 76.88 m³ usable.
"""

from __future__ import annotations

import datetime
import math

import pytest

from agri.core.water_level import (
    DeviceConfig,
    ManualAdapter,
    SimulatorAdapter,
    WaterLevelError,
    WaterLevelReading,
    WaterSourceSpec,
    active_alert_kinds,
    capacity_m3,
    compute_metrics,
    data_status,
    get_adapter,
    level_state,
    normalize_level,
    validate_calibration_table,
    validate_spec,
    volume_m3,
)

NOW = datetime.datetime(2026, 8, 6, 15, 0, tzinfo=datetime.UTC)

BASIN = WaterSourceSpec(
    source_type="BASIN",
    geometry="RECTANGULAR",
    max_height_m=4.80,
    length_m=8.0,
    width_m=3.0,
    minimum_reserve_m3=10.0,
)
TANK = WaterSourceSpec(source_type="TANK", geometry="CYLINDRICAL", max_height_m=2.0, diameter_m=2.0)
POND = WaterSourceSpec(
    source_type="BASIN",
    geometry="IRREGULAR",
    max_height_m=2.0,
    max_capacity_m3=500.0,
    calibration_table=[
        {"level_m": 2.0, "volume_m3": 500.0},
        {"level_m": 0.0, "volume_m3": 0.0},
        {"level_m": 1.0, "volume_m3": 150.0},
    ],
)
WELL = WaterSourceSpec(
    source_type="WELL", geometry="CYLINDRICAL", max_height_m=30.0, total_depth_m=80.0
)


def _reading(minutes_ago: float, *, mode="SIMULATED", quality="GOOD"):
    return WaterLevelReading(
        level_m=1.0,
        measured_at=NOW - datetime.timedelta(minutes=minutes_ago),
        device_id="sim",
        mode=mode,
        quality=quality,
    )


class TestBriefExample:
    def test_metrics(self):
        m = compute_metrics(BASIN, 3.62)
        assert m.volume_m3 == pytest.approx(86.88)
        assert m.capacity_m3 == pytest.approx(115.20)
        assert m.free_capacity_m3 == pytest.approx(28.32)
        assert m.available_m3 == pytest.approx(76.88)
        assert round(m.fill_percent, 1) == 75.4
        assert m.level_state == "NORMAL"
        assert m.depth_to_water_m is None

    def test_available_never_negative(self):
        assert compute_metrics(BASIN, 0.1).available_m3 == 0.0


class TestVolume:
    def test_cylinder(self):
        assert volume_m3(TANK, 1.0) == pytest.approx(math.pi)
        assert capacity_m3(TANK) == pytest.approx(2 * math.pi)

    def test_irregular_interpolates_sorted_table(self):
        assert volume_m3(POND, 0.5) == pytest.approx(75.0)
        assert volume_m3(POND, 1.5) == pytest.approx(325.0)
        assert capacity_m3(POND) == 500.0

    def test_level_is_clamped(self):
        assert volume_m3(BASIN, 99) == pytest.approx(115.2)
        assert volume_m3(BASIN, -1) == 0.0

    def test_well_without_diameter_has_no_volume(self):
        m = compute_metrics(WELL, 12.0)
        assert m.volume_m3 is None
        assert m.available_m3 is None
        assert m.free_capacity_m3 is None
        assert m.depth_to_water_m == pytest.approx(68.0)
        assert m.fill_percent == pytest.approx(40.0)


class TestNormalize:
    def test_scale_offset_and_clamp(self):
        assert normalize_level(250, 4.8, scale=0.01, offset_m=0.1) == (2.6, "GOOD")
        assert normalize_level(4.85, 4.8) == (4.8, "GOOD")

    def test_far_out_of_range_is_invalid(self):
        assert normalize_level(6.0, 4.8) == (4.8, "INVALID")
        assert normalize_level(-1.0, 4.8) == (0.0, "INVALID")

    def test_nan_rejected(self):
        with pytest.raises(WaterLevelError):
            normalize_level(float("nan"), 4.8)


class TestValidation:
    def test_valid_specs(self):
        for spec in (BASIN, TANK, POND, WELL):
            validate_spec(spec)

    @pytest.mark.parametrize(
        "changes",
        [
            {"max_height_m": 0},
            {"length_m": None},
            {"source_type": "LAKE"},
            {"critical_pct": 40},
            {"high_pct": 20},
            {"minimum_reserve_m3": -1},
            {"total_depth_m": 1.0},
            {"offline_after_min": 0},
        ],
    )
    def test_invalid_basin(self, changes):
        with pytest.raises(WaterLevelError):
            validate_spec(WaterSourceSpec(**{**BASIN.__dict__, **changes}))

    def test_irregular_requires_table(self):
        with pytest.raises(WaterLevelError):
            validate_spec(WaterSourceSpec(**{**POND.__dict__, "calibration_table": None}))

    @pytest.mark.parametrize(
        "table",
        [
            [{"level_m": 0, "volume_m3": 0}],
            [{"level_m": 0, "volume_m3": 5}, {"level_m": 1, "volume_m3": 2}],
            [{"level_m": 1, "volume_m3": 0}, {"level_m": 1, "volume_m3": 2}],
            [{"level_m": "x", "volume_m3": 0}, {"level_m": 1, "volume_m3": 2}],
        ],
    )
    def test_bad_calibration_table(self, table):
        with pytest.raises(WaterLevelError):
            validate_calibration_table(table)


class TestStatusAndAlerts:
    def test_status_rules(self):
        assert data_status(BASIN, None, NOW) == "OFFLINE"
        assert data_status(BASIN, _reading(5), NOW) == "SIMULATED"
        assert data_status(BASIN, _reading(5, mode="LIVE"), NOW) == "LIVE"
        assert data_status(BASIN, _reading(61), NOW) == "STALE"
        assert data_status(BASIN, _reading(121), NOW) == "OFFLINE"
        assert data_status(BASIN, _reading(5, quality="INVALID"), NOW) == "INVALID"
        calibrating = WaterSourceSpec(**{**BASIN.__dict__, "mode": "CALIBRATING"})
        assert data_status(calibrating, _reading(5), NOW) == "CALIBRATING"

    def test_level_states(self):
        assert level_state(10, BASIN) == "CRITICAL"
        assert level_state(15, BASIN) == "CRITICAL"
        assert level_state(25, BASIN) == "LOW"
        assert level_state(50, BASIN) == "NORMAL"
        assert level_state(96, BASIN) == "HIGH"
        assert level_state(None, BASIN) == "UNKNOWN"

    def test_alert_kinds(self):
        assert active_alert_kinds(BASIN, 10, "SIMULATED") == {"CRITICAL"}
        assert active_alert_kinds(BASIN, 25, "LIVE") == {"LOW"}
        assert active_alert_kinds(BASIN, 99, "STALE") == {"HIGH"}
        assert active_alert_kinds(BASIN, 50, "LIVE") == set()
        assert active_alert_kinds(BASIN, 10, "OFFLINE") == {"OFFLINE"}
        assert active_alert_kinds(BASIN, 10, "INVALID") == set()


class TestAdapters:
    def test_simulator(self):
        r = get_adapter("simulator").normalize(
            {"level_m": 3.62, "measured_at": "2026-08-06T15:00:00Z"},
            DeviceConfig(device_id="simulator_1", scale=100, offset_m=5),
            BASIN,
            now=NOW,
        )
        # Device scale/offset never apply to the simulator: it sends metres.
        assert r.level_m == 3.62
        assert r.mode == "SIMULATED"
        assert r.measured_at == NOW
        assert r.raw_payload is None

    def test_manual_raw_with_scale(self):
        r = ManualAdapter().normalize(
            {"raw_level": 362, "rssi": -90, "battery_percent": "88"},
            DeviceConfig(device_id="dev-1", scale=0.01),
            BASIN,
            now=NOW,
        )
        assert r.level_m == pytest.approx(3.62)
        assert r.mode == "LIVE"
        assert r.measured_at == NOW
        assert (r.rssi, r.battery_percent) == (-90.0, 88.0)
        assert r.raw_payload == {"raw_level": 362, "rssi": -90, "battery_percent": "88"}

    def test_errors(self):
        with pytest.raises(WaterLevelError):
            get_adapter("dragino")
        with pytest.raises(WaterLevelError):
            SimulatorAdapter().normalize(
                {"level_m": "high"}, DeviceConfig(device_id="s"), BASIN, now=NOW
            )
        with pytest.raises(WaterLevelError):
            SimulatorAdapter().normalize(
                {"level_m": 1, "measured_at": "yesterday"},
                DeviceConfig(device_id="s"),
                BASIN,
                now=NOW,
            )
