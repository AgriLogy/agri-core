"""Per-sector decision layer for the farm map (agri-api#459, phase 3).

Pure functions only — no database, no HTTP. The agri-api adapter fetches the
sector's zones, Kc table, past weather (ET₀ + rain), irrigation volumes, soil
moisture and stored NDVI, and hands them here.

Three pieces:

* :func:`kc_for_day`        — the farmer's Kc table entry for a date, else 1.0.
* :func:`run_water_balance` — FAO-56 root-zone depletion (Dr) rolled day by day
  from an assumed field-capacity start, clamped to ``[0, TAW]``.
* :func:`recommend`         — irrigate / wait / nothing, through the existing
  :func:`agri.core.agronomy.irrigation_decision_dr` (same rules as the daily
  email), with a message in French, Arabic or English.
* :func:`interpret_vegetation` — NDVI level + trend read together with soil
  moisture → healthy / water stress / excess water / low vigour (non-water) /
  declining / insufficient data.

The vegetation rules are agronomic heuristics, not a diagnosis: NDVI is canopy
vigour from 10 m pixels. Every message says what it is based on, and the
thresholds below are named so an agronomist can tune them.

Messages and labels come from :mod:`agri.core.sector_insights_i18n`
(``lang="fr" | "ar" | "en"``, French by default; codes never change).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from agri.core.agronomy import (
    DEFAULT_CRITICAL_SOIL_MOISTURE_PCT,
    DEFAULT_IRRIGATION_EFFICIENCY,
    DEFAULT_KC,
    DEFAULT_RAINFALL_EFFICIENCY,
    effective_rainfall_mm,
    etc_mm,
    irrigation_decision_dr,
    update_daily_depletion,
)
from agri.core.sector_insights_i18n import (
    LABELS,
    MESSAGES,
    SUPPORTED_LANGS,
    fmt_day,
    fmt_num,
    normalize_lang,
)

__all__ = [
    "IRRIGATE_SOON_RATIO",
    "NDVI_DECLINE_PER_WEEK",
    "NDVI_LOW_VIGOR",
    "NDVI_MAX_AGE_DAYS",
    "SUPPORTED_LANGS",
    "WET_MARGIN_PCT",
    "BalanceDay",
    "BalanceRow",
    "KcPeriod",
    "balance_status",
    "fmt_day",
    "fmt_fr",
    "fmt_num",
    "irrigation_depth_mm",
    "interpret_vegetation",
    "kc_for_day",
    "ndvi_trend_per_week",
    "normalize_lang",
    "recommend",
    "run_water_balance",
]

IRRIGATE_SOON_RATIO = 0.75
"""Dr ≥ this share of RAW → "irrigate soon" (before the stress threshold)."""

NDVI_LOW_VIGOR = 0.30
"""Mean NDVI under this over a cropped sector reads as low vigour."""

NDVI_DECLINE_PER_WEEK = -0.03
"""A linear NDVI trend at or below this (per week) counts as declining."""

NDVI_STABLE_PER_WEEK = 0.01
"""A trend smaller than this (either way, per week) is reported as stable."""

NDVI_MAX_AGE_DAYS = 30
"""An NDVI value older than this is too stale to interpret."""

NDVI_TREND_WINDOW_DAYS = 35
"""Window of recent NDVI points the trend is fitted on."""

WET_MARGIN_PCT = 20.0
"""Soil moisture ≥ critical threshold + this margin reads as (too) wet."""


# ---------------------------------------------------------------------------
# Number formatting (French helper; per-language in sector_insights_i18n)
# ---------------------------------------------------------------------------


def fmt_fr(value: float, decimals: int = 1) -> str:
    """``12.5`` → ``"12,5"``; trailing ``,0`` dropped (``"12"``).

    Kept as-is for existing callers; :func:`fmt_num` is the per-language form.
    """
    text = f"{value:.{decimals}f}"
    if decimals and text.endswith("." + "0" * decimals):
        text = text[: -(decimals + 1)]
    return text.replace(".", ",").replace("-", "−")


# ---------------------------------------------------------------------------
# Kc
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KcPeriod:
    """One stage of the farmer's Kc table (``analytics_kcperiod``)."""

    name: str
    start: date
    end: date
    kc: float


def kc_for_day(periods: Sequence[KcPeriod], day: date) -> tuple[float, str, str | None]:
    """``(kc, source, period_name)`` for ``day``.

    ``source`` is ``"kc_period"`` when a period covers the day (inclusive
    bounds; earliest-starting wins on overlap), else ``"default"`` with
    :data:`agri.core.agronomy.DEFAULT_KC`.
    """
    for period in sorted(periods, key=lambda p: p.start):
        if period.start <= day <= period.end and period.kc > 0:
            return period.kc, "kc_period", period.name
    return DEFAULT_KC, "default", None


# ---------------------------------------------------------------------------
# Water balance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BalanceDay:
    """One day of inputs. ``et0_mm`` None = unknown → no crop use counted."""

    day: date
    et0_mm: float | None
    rain_mm: float
    irrigation_mm: float
    kc: float


@dataclass(frozen=True)
class BalanceRow:
    day: date
    et0_mm: float | None
    kc: float
    etc_mm: float
    rain_mm: float
    irrigation_mm: float
    dr_mm: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "et0_mm": None if self.et0_mm is None else round(self.et0_mm, 2),
            "kc": round(self.kc, 3),
            "etc_mm": round(self.etc_mm, 2),
            "rain_mm": round(self.rain_mm, 2),
            "irrigation_mm": round(self.irrigation_mm, 2),
            "dr_mm": round(self.dr_mm, 2),
        }


def irrigation_depth_mm(
    volume_l: float,
    area_m2: float | None,
    efficiency: float = DEFAULT_IRRIGATION_EFFICIENCY,
) -> float:
    """Net water depth reaching the root zone: litres / m² (= mm) × efficiency."""
    if not area_m2 or area_m2 <= 0 or volume_l <= 0:
        return 0.0
    return volume_l / area_m2 * efficiency


def run_water_balance(
    days: Sequence[BalanceDay],
    *,
    taw_mm: float,
    dr_start_mm: float = 0.0,
    alpha_rain: float = DEFAULT_RAINFALL_EFFICIENCY,
) -> list[BalanceRow]:
    """Roll Dr forward over ``days`` (oldest first), FAO-56 eq. 85.

    ``Dr_i = Dr_(i-1) + ETc_i − Pe_i − I_i``, clamped to ``[0, TAW]``: the soil
    cannot be wetter than field capacity nor drier than the wilting point.
    """
    dr = max(0.0, min(dr_start_mm, taw_mm))
    rows: list[BalanceRow] = []
    for d in sorted(days, key=lambda x: x.day):
        etc = etc_mm(d.et0_mm, d.kc) if d.et0_mm is not None else 0.0
        dr = update_daily_depletion(
            dr_yesterday_mm=dr,
            etc_today_mm=etc,
            pe_today_mm=effective_rainfall_mm(max(0.0, d.rain_mm), alpha_rain),
            irrigation_applied_mm=max(0.0, d.irrigation_mm),
        )
        dr = min(dr, taw_mm)
        rows.append(
            BalanceRow(
                day=d.day,
                et0_mm=d.et0_mm,
                kc=d.kc,
                etc_mm=etc,
                rain_mm=max(0.0, d.rain_mm),
                irrigation_mm=max(0.0, d.irrigation_mm),
                dr_mm=dr,
            )
        )
    return rows


def balance_status(dr_mm: float, raw_mm: float) -> str:
    """``irrigate_now`` (Dr ≥ RAW) | ``irrigate_soon`` (≥ 75 % RAW) | ``ok``."""
    if raw_mm > 0 and dr_mm >= raw_mm:
        return "irrigate_now"
    if raw_mm > 0 and dr_mm >= IRRIGATE_SOON_RATIO * raw_mm:
        return "irrigate_soon"
    return "ok"


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def recommend(
    *,
    dr_mm: float,
    raw_mm: float,
    soil_moisture_pct: float | None,
    critical_moisture_pct: float | None,
    area_m2: float | None,
    pump_flow_l_per_s: float | None,
    max_water_per_day_l: float | None,
    rain_next_48h_mm: float,
    lang: str = "fr",
) -> dict[str, Any]:
    """Irrigate / wait / nothing, with volume + duration when computable.

    ``lang`` (fr | ar | en, unknown → fr) only changes ``message``.

    Uses :func:`irrigation_decision_dr` (the rules of the daily email). The
    trigger does not depend on the sector's area or pump; only the volume and
    duration do, so those are ``None`` when area or flow is unknown and the
    message gives the net depth in mm instead.
    """
    critical = (
        critical_moisture_pct
        if critical_moisture_pct is not None
        else DEFAULT_CRITICAL_SOIL_MOISTURE_PCT
    )
    has_volume = bool(area_m2 and area_m2 > 0 and pump_flow_l_per_s and pump_flow_l_per_s > 0)
    decision = irrigation_decision_dr(
        dr_today_mm=dr_mm,
        raw_mm=raw_mm,
        soil_moisture_pct=soil_moisture_pct,
        critical_moisture_pct=critical,
        zone_area_m2=area_m2 if has_volume else 1.0,
        flow_rate_m3h=(pump_flow_l_per_s or 1.0) * 3.6,
        max_water_per_day_m3=(max_water_per_day_l or 0.0) / 1000.0 if has_volume else 0.0,
        precipitation_forecast_mm=rain_next_48h_mm,
    )
    volume = round(decision.volume_m3, 2) if has_volume and decision.irrigate else None
    duration = round(decision.duration_hr * 60.0, 0) if has_volume and decision.irrigate else None

    lang = normalize_lang(lang)
    t = MESSAGES[lang]

    def n(value: float, decimals: int = 1) -> str:
        return fmt_num(value, decimals, lang)

    if decision.irrigate:
        action = "irrigate"
        if volume is not None and duration is not None:
            amount = t["amount_volume"].format(volume=n(volume), duration=n(duration, 0))
        else:
            amount = t["amount_depth"].format(depth=n(decision.gross_mm))
        why_template = t.get(f"why_{decision.reason}")
        why = (
            why_template.format(
                dr=n(dr_mm),
                raw=n(raw_mm),
                moisture=n(soil_moisture_pct or 0.0, 0),
                critical=n(critical, 0),
                rain=n(rain_next_48h_mm),
            )
            if why_template is not None
            else decision.reason
        )
        key = "irrigate_complementary" if decision.reason == "complementary" else "irrigate"
        message = t[key].format(amount=amount, why=why)
        if decision.capped_to_daily_max:
            message += t["capped"]
        if volume is None:
            message += t["need_area_flow"]
    elif decision.reason == "rain_will_suffice":
        action = "wait"
        message = t["wait"].format(rain=n(rain_next_48h_mm))
    else:
        action = "none"
        message = t["none"].format(dr=n(dr_mm), raw=n(raw_mm))

    return {
        "action": action,
        "reason": decision.reason,
        "volume_m3": volume,
        "duration_min": duration,
        "net_mm": round(decision.net_mm, 2),
        "rain_next_48h_mm": round(rain_next_48h_mm, 2),
        "message": message,
    }


# ---------------------------------------------------------------------------
# NDVI + soil moisture interpretation
# ---------------------------------------------------------------------------


def ndvi_trend_per_week(
    points: Sequence[tuple[date, float]],
    *,
    today: date,
    window_days: int = NDVI_TREND_WINDOW_DAYS,
) -> tuple[float | None, int]:
    """Least-squares NDVI slope per week over the last ``window_days``.

    Returns ``(slope, n_points)``; slope is None with fewer than 3 points or a
    span shorter than 10 days (too little to call a trend).
    """
    start = today - timedelta(days=window_days)
    recent = sorted((d, v) for d, v in points if start <= d <= today)
    n = len(recent)
    if n < 3 or (recent[-1][0] - recent[0][0]).days < 10:
        return None, n
    xs = [(d - recent[0][0]).days for d, _ in recent]
    ys = [v for _, v in recent]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, n
    slope_per_day = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return round(slope_per_day * 7.0, 3), n


_VEG_LABELS = LABELS["fr"]  # kept for importers of the French labels


def interpret_vegetation(
    *,
    ndvi_points: Sequence[tuple[date, float]],
    soil_moisture_pct: float | None,
    critical_moisture_pct: float | None,
    depletion_ratio: float | None,
    today: date,
    lang: str = "fr",
) -> dict[str, Any]:
    """Read NDVI (level + trend) together with soil water.

    ``lang`` (fr | ar | en, unknown → fr) only changes ``label`` and ``message``.

    ``depletion_ratio`` is Dr / RAW from the water balance (None when not
    computed); it stands in for a missing soil-moisture probe.
    """
    lang = normalize_lang(lang)
    t = MESSAGES[lang]
    critical = (
        critical_moisture_pct
        if critical_moisture_pct is not None
        else DEFAULT_CRITICAL_SOIL_MOISTURE_PCT
    )
    fresh = [(d, v) for d, v in ndvi_points if (today - d).days <= NDVI_MAX_AGE_DAYS]
    latest = max(fresh, key=lambda p: p[0]) if fresh else None
    trend, n_points = ndvi_trend_per_week(ndvi_points, today=today)

    # Soil water state: probe first, water balance as a fallback.
    if soil_moisture_pct is not None:
        dry = soil_moisture_pct < critical
        wet = soil_moisture_pct >= critical + WET_MARGIN_PCT
        soil_basis = t["basis_soil_probe"].format(moisture=fmt_num(soil_moisture_pct, 0, lang))
    elif depletion_ratio is not None:
        dry = depletion_ratio >= 1.0
        wet = False
        soil_basis = t["basis_soil_balance"].format(pct=fmt_num(depletion_ratio * 100, 0, lang))
    else:
        dry = wet = False
        soil_basis = None

    low = latest is not None and latest[1] < NDVI_LOW_VIGOR
    declining = trend is not None and trend <= NDVI_DECLINE_PER_WEEK
    weak = low or declining

    parts: list[str] = []
    if latest is not None:
        parts.append(
            t["basis_ndvi"].format(ndvi=fmt_num(latest[1], 2, lang), day=fmt_day(latest[0], lang))
        )
    if trend is not None:
        if abs(trend) < NDVI_STABLE_PER_WEEK:
            parts.append(t["basis_trend_stable"])
        else:
            sign = "+" if trend > 0 else ""
            parts.append(t["basis_trend"].format(trend=f"{sign}{fmt_num(trend, 2, lang)}"))
    if soil_basis:
        parts.append(soil_basis)
    basis = t["basis_sep"].join(parts)

    if latest is None and soil_basis is None:
        status = "insufficient_data"
        message = t["veg_no_data"]
        confidence = "low"
    elif latest is None:
        status = "water_stress" if dry else "insufficient_data"
        message = t["veg_no_image"].format(soil=soil_basis) + (t["veg_no_image_dry"] if dry else "")
        confidence = "low"
    elif weak and dry:
        status = "water_stress"
        message = t["veg_stress"].format(basis=basis)
        confidence = "high" if soil_moisture_pct is not None and trend is not None else "medium"
    elif weak and wet:
        status = "excess_water"
        message = t["veg_excess"].format(basis=basis)
        confidence = "medium"
    elif weak and soil_basis is not None:
        status = "low_vigor_other"
        message = t["veg_other_cause"].format(basis=basis)
        confidence = "medium"
    elif weak:
        status = "declining" if declining else "low_vigor_other"
        message = t["veg_no_soil"].format(basis=basis)
        confidence = "low"
    else:
        status = "healthy"
        message = t["veg_healthy"].format(basis=basis)
        if dry:
            message += t["veg_healthy_dry"]
        confidence = "medium" if trend is None else "high"

    return {
        "status": status,
        "label": LABELS[lang][status],
        "message": message[0].upper() + message[1:],
        "confidence": confidence,
        "ndvi": (
            {
                "date": latest[0].isoformat(),
                "mean": round(latest[1], 3),
                "trend_per_week": trend,
                "points": n_points,
            }
            if latest is not None
            else None
        ),
        "critical_moisture_pct": critical,
    }
