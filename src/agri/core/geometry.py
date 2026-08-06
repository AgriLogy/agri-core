"""GeoJSON polygon geometry — framework-agnostic, pure.

Business-logic half of the sector-geometry move (agri-db ``analytics_sector``
gains ``geometry``/``area_ha``/``perimeter_m``). Sector shapes used to live in
the browser's ``localStorage``; once they are server-owned, the *server* must
own their derived numbers too — a client-computed area drifts from the stored
shape the moment the two disagree, and the whole point of moving is to have one
answer.

Owns:

* ``validate_polygon`` — structural validation of a GeoJSON Polygon /
  MultiPolygon, returning the normalized geometry;
* ``area_m2`` / ``area_hectares`` — geodesic area, holes subtracted;
* ``perimeter_m`` — geodesic length of the outer boundary;
* ``centroid`` — area-weighted centroid, for "zoom to this sector".

Never touches the database and never mutates its input.

**Sphere, not ellipsoid.** Areas use the spherical-excess formula and lengths
use haversine, both on the mean Earth radius. Against WGS84 that is worth
roughly 0.1-0.3% at Moroccan latitudes — centimetres on a field boundary and
far below the GPS error of a hand-drawn map polygon. It buys a module with no
dependency on shapely/pyproj/GEOS, which is what lets this live in a pure lib
that both the API and any future worker can import. If sub-0.1% ever matters,
swap the two primitives here; nothing else needs to change.

**Coordinate order is GeoJSON's: ``[longitude, latitude]``, degrees.** Getting
this backwards is the classic geo bug, so ``validate_polygon`` range-checks
both and rejects a latitude outside ±90 — which is what a swapped pair looks
like for most of the world.
"""

from __future__ import annotations

import math
from typing import Any

# Mean Earth radius (IUGG), metres. The same constant for area and length so
# the two can never disagree about what planet they are on.
EARTH_RADIUS_M = 6371008.8

# A GeoJSON linear ring needs 4 positions: 3 distinct corners + the repeated
# closing point. Anything less cannot enclose an area.
_MIN_RING_POSITIONS = 4

__all__ = [
    "EARTH_RADIUS_M",
    "GeometryError",
    "area_hectares",
    "area_m2",
    "centroid",
    "perimeter_m",
    "validate_polygon",
]


class GeometryError(ValueError):
    """Raised when a value is not a usable GeoJSON Polygon / MultiPolygon.

    A ``ValueError`` subclass so callers that already treat bad input as a
    400 need no new except clause.
    """


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_ring(ring: Any, where: str) -> list[list[float]]:
    if not isinstance(ring, (list, tuple)) or len(ring) < _MIN_RING_POSITIONS:
        raise GeometryError(
            f"{where}: a linear ring needs at least {_MIN_RING_POSITIONS} "
            f"positions (3 corners + the closing point)."
        )

    out: list[list[float]] = []
    for i, pos in enumerate(ring):
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise GeometryError(f"{where}[{i}]: a position needs [lon, lat].")
        lon, lat = pos[0], pos[1]
        if isinstance(lon, bool) or isinstance(lat, bool):
            raise GeometryError(f"{where}[{i}]: coordinates must be numbers.")
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            raise GeometryError(f"{where}[{i}]: coordinates must be numbers.")
        if math.isnan(lon) or math.isnan(lat) or math.isinf(lon) or math.isinf(lat):
            raise GeometryError(f"{where}[{i}]: coordinates must be finite.")
        if not -180.0 <= lon <= 180.0:
            raise GeometryError(f"{where}[{i}]: longitude {lon} outside [-180, 180].")
        if not -90.0 <= lat <= 90.0:
            raise GeometryError(
                f"{where}[{i}]: latitude {lat} outside [-90, 90] "
                f"(GeoJSON order is [longitude, latitude] — are they swapped?)."
            )
        out.append([float(lon), float(lat)])

    # Close it if the client left it open. Mapbox Draw always closes its rings,
    # but a hand-built GeoJSON import often does not, and an unclosed ring
    # silently loses one edge from both area and perimeter.
    if out[0] != out[-1]:
        out.append(list(out[0]))
        if len(out) < _MIN_RING_POSITIONS:
            raise GeometryError(
                f"{where}: a linear ring needs at least {_MIN_RING_POSITIONS} "
                f"positions once closed."
            )
    return out


def validate_polygon(geometry: Any) -> dict[str, Any]:
    """Validate a GeoJSON Polygon / MultiPolygon and return it normalized.

    Normalized means: coordinates coerced to ``float``, every ring explicitly
    closed, and only the ``type``/``coordinates`` members kept. Raises
    :class:`GeometryError` on anything else — including the ``Feature`` wrapper,
    which is a common paste mistake and gets its own message.
    """
    if not isinstance(geometry, dict):
        raise GeometryError("Geometry must be a GeoJSON object.")

    gtype = geometry.get("type")
    if gtype == "Feature":
        raise GeometryError("Expected a bare geometry, not a Feature — send feature.geometry.")
    if gtype not in ("Polygon", "MultiPolygon"):
        raise GeometryError(
            f"Unsupported geometry type {gtype!r}; expected Polygon or MultiPolygon."
        )

    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)) or not coords:
        raise GeometryError(f"{gtype} needs a non-empty 'coordinates' array.")

    if gtype == "Polygon":
        rings = [_validate_ring(r, f"coordinates[{i}]") for i, r in enumerate(coords)]
        return {"type": "Polygon", "coordinates": rings}

    polygons: list[list[list[list[float]]]] = []
    for pi, poly in enumerate(coords):
        if not isinstance(poly, (list, tuple)) or not poly:
            raise GeometryError(f"coordinates[{pi}]: each polygon needs its rings.")
        polygons.append(
            [_validate_ring(r, f"coordinates[{pi}][{ri}]") for ri, r in enumerate(poly)]
        )
    return {"type": "MultiPolygon", "coordinates": polygons}


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def _ring_area_m2(ring: list[list[float]]) -> float:
    """Signed spherical-excess area of one closed ring, in m².

    Sign follows winding (positive counter-clockwise), which is what lets
    :func:`area_m2` subtract holes without caring how they were wound.
    """
    total = 0.0
    for i in range(len(ring) - 1):
        lon1, lat1 = ring[i]
        lon2, lat2 = ring[i + 1]
        # Normalize the longitude step into [-180, 180] so a polygon that
        # happens to straddle the antimeridian does not wrap the long way
        # round and report a planet-sized area.
        dlon = math.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
        total += dlon * (2.0 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return total * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0


def _ring_length_m(ring: list[list[float]]) -> float:
    """Haversine length of one closed ring, in metres."""
    total = 0.0
    for i in range(len(ring) - 1):
        lon1, lat1 = ring[i]
        lon2, lat2 = ring[i + 1]
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = p2 - p1
        dl = math.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
        a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
        total += 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))
    return total


def _polygons(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    """The geometry as a uniform list of polygons (each a list of rings)."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    return geometry["coordinates"]


# ---------------------------------------------------------------------------
# Public measures
# ---------------------------------------------------------------------------
def area_m2(geometry: Any) -> float:
    """Geodesic area in m², holes subtracted. Never negative."""
    geom = validate_polygon(geometry)
    total = 0.0
    for rings in _polygons(geom):
        if not rings:
            continue
        # abs() per ring, then outer minus holes: the caller's winding order is
        # not something a drawn-on-a-map polygon can be trusted to get right.
        outer = abs(_ring_area_m2(rings[0]))
        holes = sum(abs(_ring_area_m2(r)) for r in rings[1:])
        total += max(0.0, outer - holes)
    return total


def area_hectares(geometry: Any) -> float:
    """Geodesic area in hectares (1 ha = 10 000 m²)."""
    return area_m2(geometry) / 10_000.0


def perimeter_m(geometry: Any) -> float:
    """Geodesic length of the OUTER boundary, in metres.

    Holes are excluded on purpose: "perimeter" for a field is the fence you
    would walk, not the fence plus the edge of every pond inside it.
    """
    geom = validate_polygon(geometry)
    return sum(_ring_length_m(rings[0]) for rings in _polygons(geom) if rings)


def centroid(geometry: Any) -> tuple[float, float]:
    """Area-weighted centroid as ``(longitude, latitude)``.

    Planar centroid per outer ring, weighted by that ring's area — accurate
    well past field scale and enough to centre a map on the sector. Degenerate
    input (all points identical, so zero area) falls back to the mean position
    rather than dividing by zero.
    """
    geom = validate_polygon(geometry)
    wx = wy = wsum = 0.0
    points: list[list[float]] = []

    for rings in _polygons(geom):
        if not rings:
            continue
        ring = rings[0]
        points.extend(ring[:-1])
        a2 = 0.0
        cx = cy = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            cross = x1 * y2 - x2 * y1
            a2 += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if a2 != 0.0:
            w = abs(a2) / 2.0
            wx += (cx / (3.0 * a2)) * w
            wy += (cy / (3.0 * a2)) * w
            wsum += w

    if wsum > 0.0:
        return (wx / wsum, wy / wsum)
    if not points:
        raise GeometryError("Cannot take the centroid of an empty geometry.")
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )
