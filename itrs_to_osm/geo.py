"""Small planar geometry for trail-scale work: a local projection, polyline
linear referencing, and bounding-box helpers.

Everything the matcher needs fits in a few kilometers, so an equirectangular
projection centered on the track is accurate to well under a meter, and pure
Python is fast enough (hundreds of vertices against tens of ways).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

BBox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
XY = tuple[float, float]

# Same sphere as itrs.distance_m (haversine, R = 6371 km), so chainage in the
# local frame agrees with geodesic lengths to ~0.05%.
M_PER_DEG_LAT = 6371000.0 * math.pi / 180.0
M_PER_DEG_LON_EQUATOR = M_PER_DEG_LAT


class LocalProjection:
    """Meters east/north of an origin, good to ~0.1% within a few km."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0 = lon0
        self.lat0 = lat0
        self.kx = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
        self.ky = M_PER_DEG_LAT

    @classmethod
    def for_bbox(cls, bbox: BBox) -> LocalProjection:
        return cls((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    def to_xy(self, lon: float, lat: float) -> XY:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self.kx, self.lat0 + y / self.ky)


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def cumulative(points: list[XY]) -> list[float]:
    """Distance along the polyline at each vertex; cumulative[0] == 0."""
    out = [0.0]
    for a, b in zip(points, points[1:]):
        out.append(out[-1] + dist(a, b))
    return out


@dataclass(frozen=True)
class Projection:
    """Nearest point on a polyline to some query point."""

    distance: float  # perpendicular (or endpoint) distance, m
    along: float  # distance along the polyline from its start, m
    index: int  # vertex index i of the polyline edge (i, i+1) that was nearest
    t: float  # position within that edge, 0..1
    point: XY


def project_point(p: XY, points: list[XY], cum: list[float] | None = None) -> Projection:
    """Project p onto the polyline, returning the nearest point and its chainage."""
    if cum is None:
        cum = cumulative(points)
    if len(points) == 1:
        return Projection(dist(p, points[0]), 0.0, 0, 0.0, points[0])
    best: Projection | None = None
    px, py = p
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 == 0.0:
            t = 0.0
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / length2
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        if best is None or d < best.distance:
            along = cum[i] + t * (cum[i + 1] - cum[i])
            best = Projection(d, along, i, t, (cx, cy))
    assert best is not None
    return best


def point_at(points: list[XY], cum: list[float], along: float) -> tuple[XY, int, float]:
    """Point at chainage `along`, clamped to the polyline. Returns (xy, edge index, t)."""
    if along <= 0.0:
        return points[0], 0, 0.0
    if along >= cum[-1]:
        return points[-1], max(len(points) - 2, 0), 1.0
    # Binary search for the edge containing `along`.
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= along:
            lo = mid
        else:
            hi = mid
    span = cum[hi] - cum[lo]
    t = 0.0 if span == 0.0 else (along - cum[lo]) / span
    ax, ay = points[lo]
    bx, by = points[hi]
    return (ax + t * (bx - ax), ay + t * (by - ay)), lo, t


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; pct in 0..100. Empty input returns 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[rank]


def buffer_bbox(bbox: BBox, meters: float) -> BBox:
    """Expand a lon/lat bbox by `meters` on every side."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2
    dlat = meters / M_PER_DEG_LAT
    dlon = meters / (M_PER_DEG_LON_EQUATOR * max(math.cos(math.radians(mid_lat)), 1e-6))
    return (min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat)


def bboxes_overlap(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def union_bbox(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def merge_bboxes(bboxes: list[BBox]) -> list[BBox]:
    """Union every group of mutually overlapping boxes; keep separate ones separate.

    Tracks far apart (Copper Harbor and Visp) stay as separate downloads; a
    trail center's batch collapses into one or a few.
    """
    merged: list[BBox] = []
    for box in bboxes:
        current = box
        changed = True
        while changed:
            changed = False
            for other in list(merged):
                if bboxes_overlap(current, other):
                    merged.remove(other)
                    current = union_bbox(current, other)
                    changed = True
        merged.append(current)
    return merged


def bbox_of(points: list[tuple[float, float]]) -> BBox:
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


_NAME_STOPWORDS = frozenset({"the", "trail", "track", "path", "ctr", "pt", "part", "and", "of"})
_NON_WORD = re.compile(r"[^0-9a-zÀ-ɏ]+")


def normalize_name(name: str) -> str:
    """Casefold, strip punctuation, collapse whitespace."""
    return " ".join(_NON_WORD.split(name.casefold())).strip()


def name_tokens(name: str) -> set[str]:
    return {t for t in normalize_name(name).split() if len(t) >= 2 and t not in _NAME_STOPWORDS}


def name_similarity(a: str, b: str) -> float:
    """1.0 for an exact normalized match, otherwise the token Jaccard overlap.

    "Loam Rider" vs "Loam Rider" -> 1.0. "KPT Fish Cove Pt. 1" vs "Keweenaw
    Point Trail (Keystone - Fish Cove)" shares {fish, cove} of 8 tokens ->
    0.25. Unrelated names -> 0.0.
    """
    if not a or not b:
        return 0.0
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 1.0
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
