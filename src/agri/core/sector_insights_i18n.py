"""Message catalog for :mod:`agri.core.sector_insights` — fr, ar, en.

The farm-map advice is read by farmers, so every language says the same thing
in short sentences. French is the reference wording (and stays byte-identical
to what the module produced before translations existed).

Number formatting per language:

* ``fr`` — decimal comma, true minus sign: ``12,5``, ``−0,06``.
* ``en`` — decimal point: ``12.5``.
* ``ar`` — Western digits with a decimal point (``12.5``), as the app shows
  them; units stay in Latin script (``mm``, ``m³``, ``%``).

Dates: ``dd/mm`` for fr and ar, ``20 Nov`` for en (no locale dependency).
"""

from __future__ import annotations

from datetime import date

__all__ = [
    "DEFAULT_LANG",
    "LABELS",
    "MESSAGES",
    "SUPPORTED_LANGS",
    "fmt_day",
    "fmt_num",
    "normalize_lang",
]

DEFAULT_LANG = "fr"
SUPPORTED_LANGS = ("fr", "ar", "en")

_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def normalize_lang(lang: str | None) -> str:
    """``"AR"`` → ``"ar"``, ``"en-GB"`` → ``"en"``; unknown / empty → ``"fr"``."""
    if not lang:
        return DEFAULT_LANG
    code = str(lang).strip().lower().replace("_", "-").split("-")[0]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def fmt_num(value: float, decimals: int = 1, lang: str = DEFAULT_LANG) -> str:
    """Language-aware number: ``fr`` ``12,5`` / ``−0,06``; ``en``/``ar`` ``12.5``.

    A trailing ``.0`` (``,0``) is dropped (``12``) in every language.
    """
    text = f"{value:.{decimals}f}"
    if decimals and text.endswith("." + "0" * decimals):
        text = text[: -(decimals + 1)]
    if normalize_lang(lang) == "fr":
        return text.replace(".", ",").replace("-", "−")
    return text


def fmt_day(day: date, lang: str = DEFAULT_LANG) -> str:
    """``dd/mm`` for fr and ar, ``20 Nov`` for en."""
    if normalize_lang(lang) == "en":
        return f"{day.day} {_EN_MONTHS[day.month - 1]}"
    return day.strftime("%d/%m")


# Vegetation status → short label.
LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "healthy": "Végétation saine",
        "water_stress": "Stress hydrique probable",
        "excess_water": "Excès d'eau probable",
        "low_vigor_other": "Faible vigueur (cause non hydrique)",
        "declining": "Vigueur en baisse",
        "insufficient_data": "Données insuffisantes",
    },
    "en": {
        "healthy": "Healthy vegetation",
        "water_stress": "Probable water stress",
        "excess_water": "Probable excess water",
        "low_vigor_other": "Low vigour (not water-related)",
        "declining": "Declining vigour",
        "insufficient_data": "Insufficient data",
    },
    "ar": {
        "healthy": "نباتات سليمة",
        "water_stress": "إجهاد مائي محتمل",
        "excess_water": "فائض مائي محتمل",
        "low_vigor_other": "ضعف في النمو (سبب غير مائي)",
        "declining": "تراجع في قوة النمو",
        "insufficient_data": "بيانات غير كافية",
    },
}

# Message templates. Numbers arrive already formatted with fmt_num / fmt_day.
MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        # recommend()
        "amount_volume": "{volume} m³ (≈ {duration} min)",
        "amount_depth": "≈ {depth} mm",
        "why_stress": (
            "le déficit du sol ({dr} mm) dépasse la réserve facilement utilisable ({raw} mm)"
        ),
        "why_soil_moisture_low": (
            "l'humidité du sol ({moisture} %) est sous le seuil critique ({critical} %)"
        ),
        "why_complementary": "la pluie prévue ({rain} mm sur 48 h) ne suffira pas",
        "irrigate": "Irriguer {amount} — {why}.",
        "irrigate_complementary": "Irrigation complémentaire de {amount} — {why}.",
        "capped": " Volume plafonné au maximum journalier de la zone.",
        "need_area_flow": (
            " Renseignez la surface et le débit de la pompe pour obtenir le volume."
        ),
        "wait": "Attendre — {rain} mm de pluie prévus dans les 48 h devraient suffire.",
        "none": (
            "Pas d'irrigation nécessaire — déficit de {dr} mm, sous la "
            "réserve facilement utilisable ({raw} mm)."
        ),
        # interpret_vegetation()
        "basis_ndvi": "NDVI {ndvi} le {day}",
        "basis_trend_stable": "tendance stable",
        "basis_trend": "tendance {trend}/sem.",
        "basis_soil_probe": "humidité du sol {moisture} %",
        "basis_soil_balance": "bilan hydrique (déficit à {pct} % de la RFU)",
        "basis_sep": ", ",
        "veg_no_data": (
            "Pas d'image satellite récente ni de mesure d'humidité du sol pour ce secteur."
        ),
        "veg_no_image": "Pas d'image satellite récente (nuages ?) ; {soil}.",
        "veg_no_image_dry": " Le sol est sec : vérifier l'irrigation.",
        "veg_stress": "{basis}. Végétation affaiblie et sol sec : vérifier l'irrigation.",
        "veg_excess": (
            "{basis}. Végétation affaiblie et sol très humide : risque d'asphyxie "
            "racinaire, réduire les apports et vérifier le drainage."
        ),
        "veg_other_cause": (
            "{basis}. Le sol n'est pas sec : chercher une autre cause "
            "(nutrition, maladie, ravageurs, densité de semis)."
        ),
        "veg_no_soil": "{basis}. Pas de mesure du sol pour en trouver la cause.",
        "veg_healthy": "{basis}.",
        "veg_healthy_dry": (
            " Végétation encore vigoureuse mais sol sec : irriguer avant l'apparition du stress."
        ),
    },
    "en": {
        "amount_volume": "{volume} m³ (≈ {duration} min)",
        "amount_depth": "≈ {depth} mm",
        "why_stress": (
            "the soil water deficit ({dr} mm) exceeds the readily available water ({raw} mm)"
        ),
        "why_soil_moisture_low": (
            "soil moisture ({moisture}%) is below the critical threshold ({critical}%)"
        ),
        "why_complementary": "the forecast rain ({rain} mm over 48 h) will not be enough",
        "irrigate": "Irrigate {amount} — {why}.",
        "irrigate_complementary": "Top-up irrigation of {amount} — {why}.",
        "capped": " Volume capped at the zone's daily maximum.",
        "need_area_flow": " Enter the area and the pump flow rate to get the volume.",
        "wait": "Wait — {rain} mm of rain forecast in the next 48 h should be enough.",
        "none": (
            "No irrigation needed — deficit of {dr} mm, below the "
            "readily available water ({raw} mm)."
        ),
        "basis_ndvi": "NDVI {ndvi} on {day}",
        "basis_trend_stable": "stable trend",
        "basis_trend": "trend {trend}/week",
        "basis_soil_probe": "soil moisture {moisture}%",
        "basis_soil_balance": "water balance (deficit at {pct}% of readily available water)",
        "basis_sep": ", ",
        "veg_no_data": "No recent satellite image and no soil moisture reading for this sector.",
        "veg_no_image": "No recent satellite image (clouds?); {soil}.",
        "veg_no_image_dry": " The soil is dry: check the irrigation.",
        "veg_stress": "{basis}. Weakened vegetation and dry soil: check the irrigation.",
        "veg_excess": (
            "{basis}. Weakened vegetation and very wet soil: risk of root asphyxia, "
            "reduce watering and check the drainage."
        ),
        "veg_other_cause": (
            "{basis}. The soil is not dry: look for another cause "
            "(nutrition, disease, pests, planting density)."
        ),
        "veg_no_soil": "{basis}. No soil reading to find the cause.",
        "veg_healthy": "{basis}.",
        "veg_healthy_dry": (
            " Vegetation still vigorous but the soil is dry: irrigate before stress sets in."
        ),
    },
    "ar": {
        "amount_volume": "{volume} m³ (≈ {duration} دقيقة)",
        "amount_depth": "≈ {depth} mm",
        "why_stress": "عجز التربة ({dr} mm) يتجاوز الماء الميسّر ({raw} mm)",
        "why_soil_moisture_low": "رطوبة التربة ({moisture}%) أدنى من العتبة الحرجة ({critical}%)",
        "why_complementary": "الأمطار المتوقعة ({rain} mm خلال 48 ساعة) لن تكون كافية",
        "irrigate": "يُنصح بالري: {amount} — {why}.",
        "irrigate_complementary": "يُنصح بري تكميلي: {amount} — {why}.",
        "capped": " الكمية محدودة بالحد الأقصى اليومي للمنطقة.",
        "need_area_flow": " أدخِل المساحة وتدفق المضخة لحساب الكمية.",
        "wait": "يُنصح بالانتظار — الأمطار المتوقعة خلال 48 ساعة ({rain} mm) كافية على الأرجح.",
        "none": "لا حاجة إلى الري — العجز {dr} mm، وهو أقل من الماء الميسّر ({raw} mm).",
        "basis_ndvi": "NDVI {ndvi} بتاريخ {day}",
        "basis_trend_stable": "اتجاه مستقر",
        "basis_trend": "الاتجاه {trend} أسبوعيًا",
        "basis_soil_probe": "رطوبة التربة {moisture}%",
        "basis_soil_balance": "الميزان المائي (العجز عند {pct}% من الماء الميسّر)",
        "basis_sep": "، ",
        "veg_no_data": "لا توجد صورة قمر صناعي حديثة ولا قياس لرطوبة التربة لهذا القطاع.",
        "veg_no_image": "لا توجد صورة قمر صناعي حديثة (غيوم؟)؛ {soil}.",
        "veg_no_image_dry": " التربة جافة: تحقّق من الري.",
        "veg_stress": "{basis}. نباتات ضعيفة وتربة جافة: تحقّق من الري.",
        "veg_excess": (
            "{basis}. نباتات ضعيفة وتربة مشبعة بالماء: خطر اختناق الجذور، "
            "قلّل كميات الري وتحقّق من الصرف."
        ),
        "veg_other_cause": (
            "{basis}. التربة ليست جافة: ابحث عن سبب آخر (التغذية، الأمراض، الآفات، كثافة الزراعة)."
        ),
        "veg_no_soil": "{basis}. لا يوجد قياس للتربة لتحديد السبب.",
        "veg_healthy": "{basis}.",
        "veg_healthy_dry": " النباتات لا تزال قوية لكن التربة جافة: قم بالري قبل ظهور الإجهاد.",
    },
}
