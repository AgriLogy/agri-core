"""Tests for agri.core.geometry.

The area assertions are anchored on a closed-form value rather than on this
module's own output, so a regression in the spherical-excess sum cannot be
"confirmed" by the very code that caused it: for a lat/lon aligned quadrilateral
the exact spherical area is ``R² · Δλ · (sin φ₂ − sin φ₁)``.
"""

from __future__ import annotations

import math

import pytest

from agri.core.geometry import (
    EARTH_RADIUS_M,
    GeometryError,
    area_hectares,
    area_m2,
    centroid,
    perimeter_m,
    validate_polygon,
)


def _square(lon0: float, lat0: float, dlon: float, dlat: float) -> dict:
    """A lat/lon aligned quadrilateral, counter-clockwise and closed."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon0, lat0],
                [lon0 + dlon, lat0],
                [lon0 + dlon, lat0 + dlat],
                [lon0, lat0 + dlat],
                [lon0, lat0],
            ]
        ],
    }


def _exact_area_m2(lat0: float, dlon: float, dlat: float) -> float:
    """Closed-form spherical area of a lat/lon aligned quadrilateral."""
    return (
        EARTH_RADIUS_M
        * EARTH_RADIUS_M
        * math.radians(dlon)
        * (math.sin(math.radians(lat0 + dlat)) - math.sin(math.radians(lat0)))
    )


# ---------------------------------------------------------------------------
# Area
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lat0", [0.0, 32.2, -33.9, 60.0])
def test_area_matches_closed_form(lat0: float) -> None:
    poly = _square(-7.95, lat0, 0.01, 0.01)
    assert area_m2(poly) == pytest.approx(_exact_area_m2(lat0, 0.01, 0.01), rel=1e-6)


def test_area_shrinks_with_latitude() -> None:
    """Same degree-box is physically smaller nearer the pole."""
    equator = area_m2(_square(0.0, 0.0, 0.01, 0.01))
    morocco = area_m2(_square(0.0, 32.2, 0.01, 0.01))
    assert morocco < equator


def _metres_to_degrees(metres: float, lat: float) -> tuple[float, float]:
    """(dlon, dlat) spanning ``metres`` at ``lat`` ON THIS MODULE'S SPHERE.

    Deliberately not the familiar 110574 / 111320 ellipsoidal constants: those
    describe WGS84, and measuring a spherical model with an ellipsoidal ruler
    reports a ~0.45% error that belongs to the ruler, not the model.
    """
    deg = math.degrees(metres / EARTH_RADIUS_M)
    return deg / math.cos(math.radians(lat)), deg


def test_field_scale_area_is_plausible() -> None:
    """~1 km x 1 km near Benguerir is ~100 ha."""
    dlon, dlat = _metres_to_degrees(1000.0, 32.2)
    assert area_hectares(_square(-7.95, 32.2, dlon, dlat)) == pytest.approx(100.0, rel=1e-3)


def test_winding_order_does_not_flip_the_sign() -> None:
    ccw = _square(-7.95, 32.2, 0.01, 0.01)
    cw = {"type": "Polygon", "coordinates": [list(reversed(ccw["coordinates"][0]))]}
    assert area_m2(cw) == pytest.approx(area_m2(ccw), rel=1e-9)
    assert area_m2(cw) > 0.0


def test_holes_are_subtracted() -> None:
    outer = _square(0.0, 32.0, 0.02, 0.02)["coordinates"][0]
    hole = _square(0.005, 32.005, 0.01, 0.01)["coordinates"][0]
    solid = area_m2({"type": "Polygon", "coordinates": [outer]})
    holed = area_m2({"type": "Polygon", "coordinates": [outer, hole]})
    hole_only = area_m2({"type": "Polygon", "coordinates": [hole]})
    assert holed == pytest.approx(solid - hole_only, rel=1e-9)


def test_hole_larger_than_outer_clamps_to_zero() -> None:
    outer = _square(0.0, 32.0, 0.01, 0.01)["coordinates"][0]
    big = _square(-1.0, 31.0, 5.0, 5.0)["coordinates"][0]
    assert area_m2({"type": "Polygon", "coordinates": [outer, big]}) == 0.0


def test_multipolygon_sums_its_parts() -> None:
    a = _square(0.0, 32.0, 0.01, 0.01)["coordinates"]
    b = _square(1.0, 32.0, 0.01, 0.01)["coordinates"]
    multi = {"type": "MultiPolygon", "coordinates": [a, b]}
    expected = area_m2({"type": "Polygon", "coordinates": a}) + area_m2(
        {"type": "Polygon", "coordinates": b}
    )
    assert area_m2(multi) == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# Perimeter
# ---------------------------------------------------------------------------
def test_perimeter_of_a_km_square() -> None:
    dlon, dlat = _metres_to_degrees(1000.0, 32.2)
    assert perimeter_m(_square(-7.95, 32.2, dlon, dlat)) == pytest.approx(4000.0, rel=1e-3)


def test_perimeter_ignores_holes() -> None:
    outer = _square(0.0, 32.0, 0.02, 0.02)["coordinates"][0]
    hole = _square(0.005, 32.005, 0.01, 0.01)["coordinates"][0]
    assert perimeter_m({"type": "Polygon", "coordinates": [outer, hole]}) == pytest.approx(
        perimeter_m({"type": "Polygon", "coordinates": [outer]}), rel=1e-9
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_unclosed_ring_is_closed_and_keeps_its_area() -> None:
    closed = _square(0.0, 32.0, 0.01, 0.01)
    opened = {"type": "Polygon", "coordinates": [closed["coordinates"][0][:-1]]}
    assert validate_polygon(opened)["coordinates"][0][-1] == closed["coordinates"][0][0]
    assert area_m2(opened) == pytest.approx(area_m2(closed), rel=1e-9)


def test_feature_wrapper_is_rejected_with_a_pointed_message() -> None:
    feature = {"type": "Feature", "properties": {}, "geometry": _square(0, 32, 1, 1)}
    with pytest.raises(GeometryError, match="feature.geometry"):
        validate_polygon(feature)


def test_swapped_lat_lon_is_rejected() -> None:
    """[lat, lon] for Morocco puts 32.2 in the longitude slot and -7.95 in
    latitude — legal numbers, so only the message catches it. A latitude of
    200 must fail outright."""
    bad = {
        "type": "Polygon",
        "coordinates": [[[0, 200], [1, 200], [1, 201], [0, 201], [0, 200]]],
    }
    with pytest.raises(GeometryError, match="latitude"):
        validate_polygon(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "Point", "coordinates": [0, 0]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], ["x", 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [370, 0]]]},
        "not a geometry",
        None,
    ],
)
def test_rejects_unusable_input(bad: object) -> None:
    with pytest.raises(GeometryError):
        validate_polygon(bad)


def test_nan_is_rejected() -> None:
    bad = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [float("nan"), 0], [0, 0]]],
    }
    with pytest.raises(GeometryError):
        validate_polygon(bad)


def test_validate_returns_a_clean_copy() -> None:
    src = _square(0.0, 32.0, 0.01, 0.01)
    src["crs"] = "legacy junk"
    out = validate_polygon(src)
    assert set(out) == {"type", "coordinates"}


# ---------------------------------------------------------------------------
# Centroid
# ---------------------------------------------------------------------------
def test_centroid_of_a_square_is_its_middle() -> None:
    lon, lat = centroid(_square(-7.95, 32.2, 0.01, 0.01))
    # 1e-7 deg is ~1 cm — floating-point noise in the cross-product sum lands
    # around 1e-9 deg (~0.1 mm), which is below what any map can mean.
    assert lon == pytest.approx(-7.945, abs=1e-7)
    assert lat == pytest.approx(32.205, abs=1e-7)


def test_centroid_of_a_degenerate_ring_falls_back_to_the_mean() -> None:
    degenerate = {
        "type": "Polygon",
        "coordinates": [[[1.0, 2.0], [1.0, 2.0], [1.0, 2.0], [1.0, 2.0]]],
    }
    assert centroid(degenerate) == pytest.approx((1.0, 2.0))
