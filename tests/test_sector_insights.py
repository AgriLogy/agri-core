"""agri.core.sector_insights — Kc, water balance, recommendation, NDVI × soil."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from agri.core.et_forecast import DailyWeatherForecast, et0_forecast
from agri.core.sector_insights import (
    BalanceDay,
    KcPeriod,
    balance_status,
    fmt_fr,
    interpret_vegetation,
    irrigation_depth_mm,
    kc_for_day,
    ndvi_trend_per_week,
    recommend,
    run_water_balance,
)

TODAY = date(2026, 11, 23)


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [(12.5, 1, "12,5"), (12.0, 1, "12"), (0.426, 2, "0,43"), (-0.06, 2, "−0,06"), (45, 0, "45")],
)
def test_fmt_fr(value, decimals, expected):
    assert fmt_fr(value, decimals) == expected


# --- Kc ---------------------------------------------------------------------


def test_kc_from_covering_period():
    periods = [
        KcPeriod("Initial", date(2026, 10, 1), date(2026, 10, 31), 0.6),
        KcPeriod("Mi-saison", date(2026, 11, 1), date(2026, 12, 15), 1.15),
    ]
    assert kc_for_day(periods, TODAY) == (1.15, "kc_period", "Mi-saison")
    # Inclusive bounds.
    assert kc_for_day(periods, date(2026, 10, 31))[0] == 0.6


def test_kc_defaults_outside_periods():
    periods = [KcPeriod("Initial", date(2026, 1, 1), date(2026, 2, 1), 0.6)]
    assert kc_for_day(periods, TODAY) == (1.0, "default", None)
    assert kc_for_day([], TODAY) == (1.0, "default", None)


# --- water balance ----------------------------------------------------------


def _days(n, et0=5.0, rain=0.0, irrigation=0.0, kc=1.0):
    return [
        BalanceDay(TODAY - timedelta(days=n - 1 - i), et0, rain, irrigation, kc) for i in range(n)
    ]


def test_balance_accumulates_etc():
    rows = run_water_balance(_days(4, et0=5.0, kc=0.8), taw_mm=100.0)
    assert [r.dr_mm for r in rows] == pytest.approx([4.0, 8.0, 12.0, 16.0])
    assert rows[-1].day == TODAY


def test_balance_rain_counts_at_80_percent_and_floors_at_zero():
    days = _days(2, et0=5.0) + [BalanceDay(TODAY + timedelta(days=1), 5.0, 20.0, 0.0, 1.0)]
    rows = run_water_balance(days, taw_mm=100.0)
    # 10 mm deficit, then +5 ETc − 16 mm effective rain → floored at field capacity.
    assert rows[-1].dr_mm == 0.0


def test_balance_clamped_to_taw():
    rows = run_water_balance(_days(30, et0=8.0), taw_mm=81.0)
    assert rows[-1].dr_mm == 81.0


def test_balance_unknown_et0_adds_no_use():
    days = [BalanceDay(TODAY, None, 0.0, 0.0, 1.0)]
    rows = run_water_balance(days, taw_mm=50.0, dr_start_mm=10.0)
    assert rows[0].dr_mm == 10.0 and rows[0].etc_mm == 0.0
    assert rows[0].as_dict()["et0_mm"] is None


def test_irrigation_reduces_deficit():
    days = _days(3, et0=5.0)
    days[-1] = BalanceDay(TODAY, 5.0, 0.0, 12.0, 1.0)
    rows = run_water_balance(days, taw_mm=100.0)
    assert rows[-1].dr_mm == pytest.approx(3.0)


def test_irrigation_depth():
    # 10 m³ on 1000 m² = 10 mm gross, 8.5 mm net at 85 % efficiency.
    assert irrigation_depth_mm(10_000, 1000.0) == pytest.approx(8.5)
    assert irrigation_depth_mm(10_000, None) == 0.0
    assert irrigation_depth_mm(0, 1000.0) == 0.0


@pytest.mark.parametrize(
    ("dr", "raw", "status"),
    [(41.0, 40.5, "irrigate_now"), (31.0, 40.5, "irrigate_soon"), (10.0, 40.5, "ok"), (5, 0, "ok")],
)
def test_balance_status(dr, raw, status):
    assert balance_status(dr, raw) == status


# --- recommendation ---------------------------------------------------------


def _rec(**kw):
    base = dict(
        dr_mm=45.0,
        raw_mm=40.5,
        soil_moisture_pct=None,
        critical_moisture_pct=25.0,
        area_m2=2000.0,
        pump_flow_l_per_s=5.0,
        max_water_per_day_l=None,
        rain_next_48h_mm=0.0,
    )
    base.update(kw)
    return recommend(**base)


def test_recommend_irrigate_with_volume():
    r = _rec()
    # 45 mm net / 0.85 = 52.94 mm gross on 2000 m² = 105.88 m³ at 18 m³/h.
    assert r["action"] == "irrigate" and r["reason"] == "stress"
    assert r["volume_m3"] == pytest.approx(105.88, abs=0.01)
    assert r["duration_min"] == pytest.approx(353, abs=1)
    assert r["message"].startswith("Irriguer 105,9 m³")
    assert "40,5 mm" in r["message"]


def test_recommend_without_area_gives_depth_only():
    r = _rec(area_m2=None)
    assert r["action"] == "irrigate"
    assert r["volume_m3"] is None and r["duration_min"] is None
    assert "mm" in r["message"] and "débit de la pompe" in r["message"]


def test_recommend_waits_for_rain():
    r = _rec(dr_mm=20.0, rain_next_48h_mm=8.0)
    assert r["action"] == "wait"
    assert "8 mm" in r["message"]


def test_recommend_nothing_when_no_stress():
    r = _rec(dr_mm=10.0)
    assert r["action"] == "none" and r["volume_m3"] is None


def test_recommend_soil_probe_triggers():
    r = _rec(dr_mm=10.0, soil_moisture_pct=18.0)
    assert r["action"] == "irrigate" and r["reason"] == "soil_moisture_low"
    assert "18 %" in r["message"]


# --- NDVI × soil --------------------------------------------------------------


def _series(values, step=5):
    n = len(values)
    return [(TODAY - timedelta(days=step * (n - 1 - i)), v) for i, v in enumerate(values)]


def test_trend_needs_three_points_over_ten_days():
    assert ndvi_trend_per_week(_series([0.6, 0.55]), today=TODAY) == (None, 2)
    slope, n = ndvi_trend_per_week(_series([0.7, 0.65, 0.6, 0.55]), today=TODAY)
    assert n == 4 and slope == pytest.approx(-0.07, abs=0.001)


def test_water_stress_when_declining_and_dry():
    v = interpret_vegetation(
        ndvi_points=_series([0.7, 0.62, 0.55, 0.48]),
        soil_moisture_pct=18.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "water_stress" and v["confidence"] == "high"
    assert "18 %" in v["message"] and v["ndvi"]["points"] == 4


def test_excess_water_when_weak_and_wet():
    v = interpret_vegetation(
        ndvi_points=_series([0.28]),
        soil_moisture_pct=50.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "excess_water"


def test_low_vigor_other_when_soil_fine():
    v = interpret_vegetation(
        ndvi_points=_series([0.25]),
        soil_moisture_pct=32.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "low_vigor_other"
    assert "nutrition" in v["message"]


def test_water_balance_stands_in_for_missing_probe():
    v = interpret_vegetation(
        ndvi_points=_series([0.7, 0.6, 0.52, 0.45]),
        soil_moisture_pct=None,
        critical_moisture_pct=None,
        depletion_ratio=1.2,
        today=TODAY,
    )
    assert v["status"] == "water_stress" and v["confidence"] == "medium"
    assert "bilan hydrique" in v["message"]


def test_healthy_and_warns_when_soil_dry():
    v = interpret_vegetation(
        ndvi_points=_series([0.7, 0.71, 0.72]),
        soil_moisture_pct=20.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "healthy"
    assert "irriguer avant" in v["message"]


def test_stale_ndvi_is_ignored():
    old = [(TODAY - timedelta(days=60), 0.2)]
    v = interpret_vegetation(
        ndvi_points=old,
        soil_moisture_pct=None,
        critical_moisture_pct=None,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "insufficient_data" and v["ndvi"] is None


def test_declining_without_soil_data():
    v = interpret_vegetation(
        ndvi_points=_series([0.7, 0.62, 0.55, 0.48]),
        soil_moisture_pct=None,
        critical_moisture_pct=None,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "declining" and v["confidence"] == "low"


# --- forecast rain ------------------------------------------------------------


def test_et0_forecast_carries_rain():
    day = DailyWeatherForecast(TODAY, 20.0, 60.0, 2.0, 800.0, 1013.0, precipitation_mm=4.26)
    out = et0_forecast([day], latitude=33.9, longitude=-6.9)
    assert out[0]["rain_mm"] == 4.26
    legacy = DailyWeatherForecast(TODAY, 20.0, 60.0, 2.0, 800.0, 1013.0)
    assert et0_forecast([legacy])[0]["rain_mm"] == 0.0


def test_flat_trend_reads_as_stable():
    v = interpret_vegetation(
        ndvi_points=_series([0.66, 0.664, 0.662, 0.661]),
        soil_moisture_pct=35.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert "tendance stable" in v["message"] and "0/sem" not in v["message"]


# --- early season ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "label", "fragment"),
    [
        ("fr", "Culture en démarrage", "NDVI bas attendu à ce stade"),
        ("en", "Crop establishing", "Low NDVI is expected at this stage"),
        ("ar", "المحصول في بداية النمو", "انخفاض NDVI متوقع"),
    ],
)
def test_low_ndvi_at_crop_start_is_expected(lang, label, fragment):
    v = interpret_vegetation(
        ndvi_points=_series([0.19, 0.2, 0.2, 0.21]),
        soil_moisture_pct=35.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
        lang=lang,
        early_season=True,
    )
    assert v["status"] == "healthy" and v["label"] == label
    assert fragment in v["message"] and ".." not in v["message"]


def test_early_season_still_flags_dry_soil():
    v = interpret_vegetation(
        ndvi_points=_series([0.2, 0.2, 0.21]),
        soil_moisture_pct=15.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
        early_season=True,
    )
    assert v["status"] == "water_stress"


def test_without_early_season_low_ndvi_is_still_low_vigor():
    v = interpret_vegetation(
        ndvi_points=_series([0.14, 0.17, 0.2, 0.23]),
        soil_moisture_pct=35.0,
        critical_moisture_pct=25.0,
        depletion_ratio=None,
        today=TODAY,
    )
    assert v["status"] == "low_vigor_other"
    assert "par semaine" in v["message"] and ".." not in v["message"]
