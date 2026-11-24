"""agri.core.sector_insights — messages in French, Arabic and English.

Every message path of ``recommend()`` and ``interpret_vegetation()`` is run in
the three languages: key fragments are asserted, and en / ar are checked for
French leaking through (accented letters, French words, Latin script in ar).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from agri.core.sector_insights import (
    SUPPORTED_LANGS,
    fmt_day,
    fmt_num,
    interpret_vegetation,
    normalize_lang,
    recommend,
)
from agri.core.sector_insights_i18n import LABELS, MESSAGES

TODAY = date(2026, 11, 23)

_FRENCH_WORDS = re.compile(
    r"\b(sol|le|la|les|du|de|des|sous|sur|pas|mais|et|Irriguer|irrigation complémentaire|déficit|"
    r"pluie|tendance|humidité|Attendre|Végétation|vérifier|bilan|réserve|semaine|sem)\b"
    r"|d'|l'",
    re.IGNORECASE,
)
_FRENCH_ACCENTS = re.compile(r"[àâçéèêëîïôûùüÿœÀÂÇÉÈÊËÎÏÔÛÙÜŸŒ]")
_ARABIC = re.compile(r"[؀-ۿ]")
# Latin tokens allowed inside Arabic text: index name and units.
_AR_ALLOWED_LATIN = re.compile(r"NDVI|m³|mm")


def assert_no_french(text: str, lang: str) -> None:
    if lang == "en":
        assert not _FRENCH_ACCENTS.search(text), text
        assert not _FRENCH_WORDS.search(text), text
        assert "," not in re.sub(r", ", "", text), text  # no decimal comma
    elif lang == "ar":
        assert _ARABIC.search(text), text
        latin = _AR_ALLOWED_LATIN.sub("", text)
        assert not re.search(r"[A-Za-zÀ-ÿ]", latin), text
        assert not re.search(r"\d,\d", text), text  # Western decimal point only


# --- formatting ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "decimals", "lang", "expected"),
    [
        (12.5, 1, "fr", "12,5"),
        (12.5, 1, "en", "12.5"),
        (12.5, 1, "ar", "12.5"),
        (12.0, 1, "en", "12"),
        (-0.06, 2, "fr", "−0,06"),
        (-0.06, 2, "en", "-0.06"),
        (-0.06, 2, "ar", "-0.06"),
        (0.426, 2, "ar", "0.43"),
        (45, 0, "en", "45"),
        (12.5, 1, "xx", "12,5"),
    ],
)
def test_fmt_num(value, decimals, lang, expected):
    assert fmt_num(value, decimals, lang) == expected


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("fr", "20/11"), ("ar", "20/11"), ("en", "20 Nov"), ("de", "20/11")],
)
def test_fmt_day(lang, expected):
    assert fmt_day(date(2026, 11, 20), lang) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fr", "fr"),
        ("ar", "ar"),
        ("en", "en"),
        ("AR", "ar"),
        ("en-GB", "en"),
        ("de", "fr"),
        ("", "fr"),
        (None, "fr"),
    ],
)
def test_normalize_lang(raw, expected):
    assert normalize_lang(raw) == expected


def test_catalogs_have_the_same_keys():
    assert set(SUPPORTED_LANGS) == set(MESSAGES) == set(LABELS)
    for lang in SUPPORTED_LANGS:
        assert MESSAGES[lang].keys() == MESSAGES["fr"].keys(), lang
        assert LABELS[lang].keys() == LABELS["fr"].keys(), lang


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_labels_are_translated(lang):
    for status, label in LABELS[lang].items():
        assert label != LABELS["fr"][status]
        assert_no_french(label, lang)


def test_label_examples():
    assert LABELS["ar"]["healthy"] == "نباتات سليمة"
    assert LABELS["ar"]["water_stress"] == "إجهاد مائي محتمل"
    assert LABELS["en"]["healthy"] == "Healthy vegetation"


# --- recommendation -----------------------------------------------------------


def _rec(lang, **kw):
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
    return recommend(**base, lang=lang)


# (case, kwargs, expected action/reason, fragments per language)
REC_CASES = [
    (
        "stress_with_volume",
        {},
        ("irrigate", "stress"),
        {
            "fr": ["Irriguer 105,9 m³ (≈ 353 min)", "déficit du sol (45 mm)", "(40,5 mm)"],
            "en": [
                "Irrigate 105.9 m³ (≈ 353 min)",
                "soil water deficit (45 mm)",
                "readily available water (40.5 mm)",
            ],
            "ar": ["يُنصح بالري: 105.9 m³ (≈ 353 دقيقة)", "عجز التربة (45 mm)", "(40.5 mm)"],
        },
    ),
    (
        "depth_only_without_area",
        {"area_m2": None},
        ("irrigate", "stress"),
        {
            "fr": ["Irriguer ≈ ", "débit de la pompe"],
            "en": ["Irrigate ≈ ", "Enter the area and the pump flow rate"],
            "ar": ["يُنصح بالري: ≈ ", "تدفق المضخة"],
        },
    ),
    (
        "complementary",
        {"rain_next_48h_mm": 8.5},
        ("irrigate", "complementary"),
        {
            "fr": ["Irrigation complémentaire de", "(8,5 mm sur 48 h)"],
            "en": ["Top-up irrigation of", "(8.5 mm over 48 h) will not be enough"],
            "ar": ["يُنصح بري تكميلي:", "(8.5 mm خلال 48 ساعة)", "لن تكون كافية"],
        },
    ),
    (
        "soil_moisture_low",
        {"dr_mm": 10.0, "soil_moisture_pct": 18.0},
        ("irrigate", "soil_moisture_low"),
        {
            "fr": ["(18 %)", "seuil critique (25 %)"],
            "en": ["soil moisture (18%)", "critical threshold (25%)"],
            "ar": ["رطوبة التربة (18%)", "العتبة الحرجة (25%)"],
        },
    ),
    (
        "capped_to_daily_max",
        {"max_water_per_day_l": 5000.0},
        ("irrigate", "stress"),
        {
            "fr": ["Irriguer 5 m³", "plafonné au maximum journalier"],
            "en": ["Irrigate 5 m³", "capped at the zone's daily maximum"],
            "ar": ["يُنصح بالري: 5 m³", "الحد الأقصى اليومي"],
        },
    ),
    (
        "wait_for_rain",
        {"dr_mm": 20.0, "rain_next_48h_mm": 12.5},
        ("wait", "rain_will_suffice"),
        {
            "fr": ["Attendre — 12,5 mm de pluie"],
            "en": ["Wait — 12.5 mm of rain", "should be enough"],
            "ar": ["يُنصح بالانتظار", "(12.5 mm)", "كافية"],
        },
    ),
    (
        "nothing_needed",
        {"dr_mm": 10.0},
        ("none", "no_stress"),
        {
            "fr": ["Pas d'irrigation nécessaire — déficit de 10 mm", "(40,5 mm)"],
            "en": ["No irrigation needed — deficit of 10 mm", "(40.5 mm)"],
            "ar": ["لا حاجة إلى الري", "10 mm", "(40.5 mm)"],
        },
    ),
]


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
@pytest.mark.parametrize(
    ("case", "kwargs", "expected", "fragments"),
    REC_CASES,
    ids=[c[0] for c in REC_CASES],
)
def test_recommend_messages(case, kwargs, expected, fragments, lang):
    r = _rec(lang, **kwargs)
    assert (r["action"], r["reason"]) == expected
    for fragment in fragments[lang]:
        assert fragment in r["message"], (lang, r["message"])
    assert_no_french(r["message"], lang)


@pytest.mark.parametrize(("case", "kwargs", "expected", "fragments"), REC_CASES)
def test_recommend_codes_and_numbers_do_not_depend_on_lang(case, kwargs, expected, fragments):
    results = [_rec(lang, **kwargs) for lang in SUPPORTED_LANGS]
    stripped = [{k: v for k, v in r.items() if k != "message"} for r in results]
    assert stripped[0] == stripped[1] == stripped[2]
    assert len({r["message"] for r in results}) == 3


def test_recommend_default_and_unknown_lang_are_french():
    assert _rec("fr") == recommend(
        dr_mm=45.0,
        raw_mm=40.5,
        soil_moisture_pct=None,
        critical_moisture_pct=25.0,
        area_m2=2000.0,
        pump_flow_l_per_s=5.0,
        max_water_per_day_l=None,
        rain_next_48h_mm=0.0,
    )
    assert _rec("de")["message"] == _rec("fr")["message"]


# --- vegetation ---------------------------------------------------------------


def _series(values, step=5):
    n = len(values)
    return [(TODAY - timedelta(days=step * (n - 1 - i)), v) for i, v in enumerate(values)]


def _veg(lang, *, ndvi=(), soil=None, critical=25.0, ratio=None):
    return interpret_vegetation(
        ndvi_points=list(ndvi),
        soil_moisture_pct=soil,
        critical_moisture_pct=critical,
        depletion_ratio=ratio,
        today=TODAY,
        lang=lang,
    )


DECLINING = _series([0.7, 0.62, 0.55, 0.48])

VEG_CASES = [
    (
        "no_data",
        {},
        "insufficient_data",
        {
            "fr": ["Pas d'image satellite récente ni de mesure"],
            "en": ["No recent satellite image and no soil moisture reading"],
            "ar": ["لا توجد صورة قمر صناعي حديثة", "رطوبة التربة"],
        },
    ),
    (
        "no_image_soil_dry",
        {"soil": 18.0},
        "water_stress",
        {
            "fr": ["(nuages ?) ; humidité du sol 18 %", "Le sol est sec"],
            "en": ["(clouds?); soil moisture 18%", "The soil is dry: check the irrigation."],
            "ar": ["(غيوم؟)؛ رطوبة التربة 18%", "التربة جافة: تحقّق من الري."],
        },
    ),
    (
        "no_image_soil_fine",
        {"soil": 32.0},
        "insufficient_data",
        {
            "fr": ["humidité du sol 32 %."],
            "en": ["soil moisture 32%."],
            "ar": ["رطوبة التربة 32%."],
        },
    ),
    (
        "water_stress_probe",
        {"ndvi": DECLINING, "soil": 18.0},
        "water_stress",
        {
            "fr": ["NDVI 0,48 le 23/11", "tendance −0,10 par semaine", "humidité du sol 18 %"],
            "en": [
                "NDVI 0.48 on 23 Nov",
                "trend -0.10/week",
                "soil moisture 18%",
                "Weakened vegetation and dry soil: check the irrigation.",
            ],
            "ar": [
                "NDVI 0.48 بتاريخ 23/11",
                "الاتجاه -0.10 أسبوعيًا",
                "رطوبة التربة 18%",
                "نباتات ضعيفة وتربة جافة",
            ],
        },
    ),
    (
        "water_stress_balance",
        {"ndvi": DECLINING, "critical": None, "ratio": 1.2},
        "water_stress",
        {
            "fr": ["bilan hydrique (déficit à 120 % de la RFU)"],
            "en": ["water balance (deficit at 120% of readily available water)"],
            "ar": ["الميزان المائي (العجز عند 120% من الماء الميسّر)"],
        },
    ),
    (
        "excess_water",
        {"ndvi": _series([0.28]), "soil": 50.0},
        "excess_water",
        {
            "fr": ["asphyxie racinaire", "drainage"],
            "en": ["very wet soil", "root asphyxia", "check the drainage"],
            "ar": ["تربة مشبعة بالماء", "اختناق الجذور", "الصرف"],
        },
    ),
    (
        "low_vigor_other",
        {"ndvi": _series([0.25]), "soil": 32.0},
        "low_vigor_other",
        {
            "fr": ["NDVI 0,25", "nutrition, maladie"],
            "en": ["NDVI 0.25", "look for another cause", "nutrition, disease, pests"],
            "ar": ["NDVI 0.25", "ابحث عن سبب آخر", "التغذية، الأمراض، الآفات"],
        },
    ),
    (
        "declining_no_soil",
        {"ndvi": DECLINING, "critical": None},
        "declining",
        {
            "fr": ["Pas de mesure du sol pour en trouver la cause."],
            "en": ["No soil reading to find the cause."],
            "ar": ["لا يوجد قياس للتربة لتحديد السبب."],
        },
    ),
    (
        "healthy_stable",
        {"ndvi": _series([0.7, 0.7, 0.705, 0.7]), "soil": 32.0},
        "healthy",
        {
            "fr": ["NDVI 0,70 le 23/11, tendance stable, humidité du sol 32 %."],
            "en": ["NDVI 0.70 on 23 Nov, stable trend, soil moisture 32%."],
            "ar": ["NDVI 0.70 بتاريخ 23/11، اتجاه مستقر، رطوبة التربة 32%."],
        },
    ),
    (
        "healthy_rising_but_dry",
        {"ndvi": _series([0.6, 0.65, 0.7, 0.75]), "soil": 20.0},
        "healthy",
        {
            "fr": ["tendance +0,07 par semaine", "irriguer avant l'apparition du stress"],
            "en": ["trend +0.07/week", "irrigate before stress sets in"],
            "ar": ["الاتجاه +0.07 أسبوعيًا", "قم بالري قبل ظهور الإجهاد"],
        },
    ),
]


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
@pytest.mark.parametrize(
    ("case", "kwargs", "status", "fragments"),
    VEG_CASES,
    ids=[c[0] for c in VEG_CASES],
)
def test_vegetation_messages(case, kwargs, status, fragments, lang):
    v = _veg(lang, **kwargs)
    assert v["status"] == status
    assert v["label"] == LABELS[lang][status]
    for fragment in fragments[lang]:
        assert fragment in v["message"], (lang, v["message"])
    assert_no_french(v["message"], lang)
    assert_no_french(v["label"], lang)


@pytest.mark.parametrize(("case", "kwargs", "status", "fragments"), VEG_CASES)
def test_vegetation_codes_do_not_depend_on_lang(case, kwargs, status, fragments):
    results = [_veg(lang, **kwargs) for lang in SUPPORTED_LANGS]
    stripped = [{k: v for k, v in r.items() if k not in ("label", "message")} for r in results]
    assert stripped[0] == stripped[1] == stripped[2]


def test_vegetation_unknown_lang_is_french():
    assert _veg("zz", ndvi=DECLINING, soil=18.0) == _veg("fr", ndvi=DECLINING, soil=18.0)
