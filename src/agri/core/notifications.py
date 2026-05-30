# ruff: noqa: E501
# Line-length is exempted in this module: the f-string template for the
# French email body intentionally keeps one line per visible row of the
# rendered email so the source matches what the user receives.
"""Notification-email composition — framework-agnostic.

Turns a :func:`agri.core.agronomy.field_snapshot` dict into the French
notification email body the Celery task sends. No Django imports; the
adapter in agri-api packs ``user_name`` from the user's profile and
calls :func:`compose_notification_email`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session


def _fmt(value: Any, suffix: str = "", precision: int = 1) -> str:
    """Render numeric values; render '—' when missing."""
    if value is None:
        return "—"
    if isinstance(value, int | float):
        return f"{value:.{precision}f}{suffix}"
    return f"{value}{suffix}"


def compose_notification_email(
    user_name: str,
    snapshot: dict[str, Any],
) -> str:
    """Render the French daily-report email body.

    ``snapshot`` must follow the contract documented on
    :func:`agri.core.agronomy.field_snapshot` (the same dict the
    Celery notification task receives).
    """
    zone_label = snapshot["zone_name"] or "votre zone"

    last_irrig_at = snapshot.get("last_irrigation_at")
    last_irrig_str = last_irrig_at.strftime("%d/%m/%Y %H:%M") if last_irrig_at else "—"
    last_irrig_volume = (
        f"{snapshot['last_irrigation_l']:.0f} L"
        if snapshot.get("last_irrigation_l") is not None
        else "—"
    )

    npk = (
        f"{_fmt(snapshot['npk_n'], precision=0)}/"
        f"{_fmt(snapshot['npk_p'], precision=0)}/"
        f"{_fmt(snapshot['npk_k'], precision=0)} mg/kg"
    )

    return f"""\
Bonjour {user_name},

Voici le rapport quotidien pour {zone_label} — {snapshot["date_today"].strftime("%d/%m/%Y")}.

Prévisions / météo (derniers 2 jours) :
🌡 Température moyenne — hier : {_fmt(snapshot["yesterday_temp_c"], " °C")} ; aujourd'hui : {_fmt(snapshot["today_temp_c"], " °C")}
💧 Humidité de l'air moyenne — hier : {_fmt(snapshot["yesterday_humidity_pct"], " %", precision=0)} ; aujourd'hui : {_fmt(snapshot["today_humidity_pct"], " %", precision=0)}
🌞 ET0 cumulée aujourd'hui : {_fmt(snapshot["et0_today_mm"], " mm", precision=2)}  (Kc utilisé : {snapshot["kc_used"]:.2f})

Dernière irrigation enregistrée :
🚰 {last_irrig_str} — volume : {last_irrig_volume}

État actuel du sol :
🌱 Humidité (couche moyenne) : {_fmt(snapshot["soil_moisture_pct"], " %", precision=0)}
🌡 Température du sol : {_fmt(snapshot["soil_temperature_c"], " °C")}
⚖️ pH : {_fmt(snapshot["soil_ph"], precision=2)}
⚡ Conductivité (EC) : {_fmt(snapshot["soil_ec"], " µS/cm", precision=0)}
🧂 Salinité (proxy) : {_fmt(snapshot["soil_salinity"], " mg/L", precision=0)}
🌿 N-P-K : {npk}

Recommandation pour aujourd'hui :
{snapshot["irrigation_decision"]}
Fenêtre d'irrigation suggérée : {snapshot["perfect_irrigation_window"]}
"""


def compose_notification_for_user(
    session: Session,
    user_id: int,
    *,
    dr_today_mm: float | None = None,
    precipitation_forecast_mm: float = 0.0,
    now: datetime | None = None,
) -> str | None:
    """DB-backed: fetch the user + their field snapshot and render the email.

    Combines :func:`agri.core.agronomy.field_snapshot_for_user` with
    :func:`compose_notification_email`. Returns ``None`` when the user
    doesn't exist. Mirrors the agri-api adapter (firstname → greeting).
    """
    from agri.core.agronomy import field_snapshot_for_user
    from agri.core.database.client import AgriMainDBClient
    from agri.db.users import CustomUserCustomuser

    user = AgriMainDBClient.get(session, CustomUserCustomuser, user_id)
    if user is None:
        return None
    snapshot = field_snapshot_for_user(
        session,
        user_id,
        dr_today_mm=dr_today_mm,
        precipitation_forecast_mm=precipitation_forecast_mm,
        now=now,
    )
    user_name = user.firstname or user.username
    return compose_notification_email(user_name, snapshot)


__all__ = ["compose_notification_email", "compose_notification_for_user"]
