"""Synthesize small OSM networks and ITRS exports that follow a path through them.

The real ITRS exports are private and never committed, so matching and
way-splitting are tested against hand-drawn networks instead. Everything is
laid out in local meters (x east, y north) around an origin near Copper
Harbor and converted to lon/lat with an equirectangular approximation.

The conversion uses the same sphere as ``itrs_to_osm.itrs.distance_m``
(R = 6371000 m), so a distance measured in local meters matches the
haversine distance the tool measures to well under a meter at these sizes.

Three layers:

- ``Network``: nodes, ways and relations, written out as Overpass-style OSM
  XML text with full ``out meta`` metadata on every element.
- ``itrs_export_along``: walks a local-meter path and emits an ITRS-shaped
  GeoJSON dict through the helpers in ``tests/conftest.py``.
- Scenario functions (``single_way_uniform`` etc): each returns
  ``(network_xml, export, expect)``, where ``expect`` is the ground truth a
  test asserts against. See ``SCENARIOS`` and the module-level comment above
  ``_expect`` for the keys.
"""

from __future__ import annotations

import math
import random
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from conftest import make_export, point_feature, segment_feature

from itrs_to_osm.itrs import TRACK_TYPE_PAVED, TRACK_TYPE_SINGLE, distance_m

EARTH_RADIUS_M = 6371000.0
M_PER_DEG_LAT = EARTH_RADIUS_M * math.pi / 180.0

TECH = "mtb:scale:itrs:technical_difficulty"
EXPOSURE = "mtb:scale:itrs:exposure"

META_TIMESTAMP = "2026-01-01T00:00:00Z"
COORD_DECIMALS = 7

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# OSM network


class Network:
    """A tiny OSM data set laid out in local meters around an origin."""

    def __init__(self, origin_lon: float = -87.80, origin_lat: float = 47.45):
        self.origin_lon = origin_lon
        self.origin_lat = origin_lat
        self.m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(origin_lat))
        self._nodes: dict[int, dict[str, Any]] = {}
        self._ways: dict[int, dict[str, Any]] = {}
        self._relations: dict[int, dict[str, Any]] = {}
        self._next_node = 1000
        self._next_way = 5000
        self._next_relation = 9000

    # coordinate conversion

    def to_lonlat(self, x_m: float, y_m: float) -> tuple[float, float]:
        lon = self.origin_lon + x_m / self.m_per_deg_lon
        lat = self.origin_lat + y_m / M_PER_DEG_LAT
        return lon, lat

    def to_local(self, lon: float, lat: float) -> Point:
        return (
            (lon - self.origin_lon) * self.m_per_deg_lon,
            (lat - self.origin_lat) * M_PER_DEG_LAT,
        )

    # elements

    @staticmethod
    def _take(store: dict[int, Any], wanted: int | None, counter: int) -> tuple[int, int]:
        if wanted is not None:
            if wanted in store:
                raise ValueError(f"id {wanted} already used")
            return wanted, counter
        while counter in store:
            counter += 1
        return counter, counter + 1

    def node(
        self,
        x_m: float,
        y_m: float,
        tags: dict[str, str] | None = None,
        *,
        id: int | None = None,
    ) -> int:
        node_id, self._next_node = self._take(self._nodes, id, self._next_node)
        lon, lat = self.to_lonlat(x_m, y_m)
        self._nodes[node_id] = {
            "lon": round(lon, COORD_DECIMALS),
            "lat": round(lat, COORD_DECIMALS),
            "xy": (x_m, y_m),
            "tags": dict(tags or {}),
        }
        return node_id

    def way(self, node_ids: list[int], tags: dict[str, str], *, id: int | None = None) -> int:
        for ref in node_ids:
            if ref not in self._nodes:
                raise ValueError(f"way references unknown node {ref}")
        if len(node_ids) < 2:
            raise ValueError("a way needs at least two nodes")
        way_id, self._next_way = self._take(self._ways, id, self._next_way)
        self._ways[way_id] = {"nodes": list(node_ids), "tags": dict(tags)}
        return way_id

    def relation(
        self,
        members: list[tuple[str, int, str]],
        tags: dict[str, str],
        *,
        id: int | None = None,
    ) -> int:
        stores = {"node": self._nodes, "way": self._ways, "relation": self._relations}
        for kind, ref, _role in members:
            if kind not in stores:
                raise ValueError(f"unknown member type {kind!r}")
            if ref not in stores[kind]:
                raise ValueError(f"relation references unknown {kind} {ref}")
        rel_id, self._next_relation = self._take(self._relations, id, self._next_relation)
        self._relations[rel_id] = {"members": list(members), "tags": dict(tags)}
        return rel_id

    def polyline(
        self,
        points_m: list[Point | int],
        tags: dict[str, str],
        *,
        spacing_m: float | None = None,
        id: int | None = None,
    ) -> int:
        """Create a way through the given points, making nodes as needed.

        Each entry is either an (x, y) local-meter point, which gets a new
        node, or an existing node id, which is reused (that is how two ways
        share a junction). With ``spacing_m``, extra nodes are inserted so no
        edge is longer than that.
        """
        refs: list[int] = []
        prev_xy: Point | None = None
        for item in points_m:
            if isinstance(item, int):
                xy = self._nodes[item]["xy"]
                ref = item
            else:
                xy = (float(item[0]), float(item[1]))
                ref = None
            if spacing_m and prev_xy is not None:
                length = math.dist(prev_xy, xy)
                pieces = max(1, math.ceil(length / spacing_m - 1e-9))
                for k in range(1, pieces):
                    t = k / pieces
                    refs.append(
                        self.node(
                            prev_xy[0] + (xy[0] - prev_xy[0]) * t,
                            prev_xy[1] + (xy[1] - prev_xy[1]) * t,
                        )
                    )
            refs.append(ref if ref is not None else self.node(*xy))
            prev_xy = xy
        return self.way(refs, tags, id=id)

    # queries

    def node_lonlat(self, node_id: int) -> tuple[float, float]:
        node = self._nodes[node_id]
        return node["lon"], node["lat"]

    def node_xy(self, node_id: int) -> Point:
        return self._nodes[node_id]["xy"]

    def way_nodes(self, way_id: int) -> list[int]:
        return list(self._ways[way_id]["nodes"])

    def way_tags(self, way_id: int) -> dict[str, str]:
        return dict(self._ways[way_id]["tags"])

    def way_lonlat(self, way_id: int) -> list[tuple[float, float]]:
        return [self.node_lonlat(n) for n in self._ways[way_id]["nodes"]]

    def way_nodes_m(self, way_id: int) -> list[tuple[int, float]]:
        """(node id, local-meter distance from the way's first node) for each node."""
        out: list[tuple[int, float]] = []
        total = 0.0
        prev: Point | None = None
        for ref in self._ways[way_id]["nodes"]:
            xy = self.node_xy(ref)
            if prev is not None:
                total += math.dist(prev, xy)
            out.append((ref, total))
            prev = xy
        return out

    def way_length_m(self, way_id: int) -> float:
        return self.way_nodes_m(way_id)[-1][1]

    def junction_nodes(self) -> set[int]:
        """Nodes used by more than one way, or more than once by the same way."""
        seen: dict[int, int] = {}
        for way in self._ways.values():
            for ref in way["nodes"]:
                seen[ref] = seen.get(ref, 0) + 1
        return {ref for ref, count in seen.items() if count > 1}

    @property
    def node_ids(self) -> list[int]:
        return sorted(self._nodes)

    @property
    def way_ids(self) -> list[int]:
        return sorted(self._ways)

    @property
    def relation_ids(self) -> list[int]:
        return sorted(self._relations)

    # output

    @staticmethod
    def _meta(element_id: int) -> str:
        return (
            f'id="{element_id}" version="1" timestamp="{META_TIMESTAMP}" '
            f'changeset="1" uid="1" user="fixture"'
        )

    @staticmethod
    def _tag_lines(tags: dict[str, str]) -> list[str]:
        return [f"    <tag k={quoteattr(k)} v={quoteattr(str(v))}/>" for k, v in tags.items()]

    def to_xml(self, *, bounds: bool = True, osm_base: str = "2026-01-01T00:00:00Z") -> str:
        """Overpass-style ``out meta`` document: nodes, then ways, then relations."""
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<osm version="0.6" generator="fixture">',
            "<note>"
            + escape("Synthesized test data for itrs-to-osm; not from OpenStreetMap.")
            + "</note>",
            f'<meta osm_base="{escape(osm_base)}"/>',
        ]
        if bounds and self._nodes:
            lons = [n["lon"] for n in self._nodes.values()]
            lats = [n["lat"] for n in self._nodes.values()]
            pad = 0.0005
            lines.append(
                f'  <bounds minlat="{min(lats) - pad:.7f}" minlon="{min(lons) - pad:.7f}" '
                f'maxlat="{max(lats) + pad:.7f}" maxlon="{max(lons) + pad:.7f}"/>'
            )
        for node_id in sorted(self._nodes):
            node = self._nodes[node_id]
            head = (
                f'  <node id="{node_id}" lat="{node["lat"]:.7f}" lon="{node["lon"]:.7f}" '
                + self._meta(node_id).split(" ", 1)[1]
            )
            if node["tags"]:
                lines.append(head + ">")
                lines.extend(self._tag_lines(node["tags"]))
                lines.append("  </node>")
            else:
                lines.append(head + "/>")
        for way_id in sorted(self._ways):
            way = self._ways[way_id]
            lines.append(f"  <way {self._meta(way_id)}>")
            lines.extend(f'    <nd ref="{ref}"/>' for ref in way["nodes"])
            lines.extend(self._tag_lines(way["tags"]))
            lines.append("  </way>")
        for rel_id in sorted(self._relations):
            rel = self._relations[rel_id]
            lines.append(f"  <relation {self._meta(rel_id)}>")
            lines.extend(
                f'    <member type="{kind}" ref="{ref}" role={quoteattr(role)}/>'
                for kind, ref, role in rel["members"]
            )
            lines.extend(self._tag_lines(rel["tags"]))
            lines.append("  </relation>")
        lines.append("</osm>")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Local-meter path helpers


def legs(start: Point, parts: list[tuple[float, float]]) -> list[Point]:
    """A polyline from ``start`` built from (length_m, compass heading in degrees) legs.

    Heading 0 is north, 90 is east.
    """
    pts = [(float(start[0]), float(start[1]))]
    for length, heading in parts:
        x, y = pts[-1]
        rad = math.radians(heading)
        pts.append((x + length * math.sin(rad), y + length * math.cos(rad)))
    return pts


def cumulative_m(path: list[Point]) -> list[float]:
    out = [0.0]
    for a, b in zip(path, path[1:]):
        out.append(out[-1] + math.dist(a, b))
    return out


def point_along(path: list[Point], d: float) -> Point:
    """The point at local-meter distance ``d`` along ``path`` (clamped to its ends)."""
    x, y, _ux, _uy = _locate(path, cumulative_m(path), d)
    return x, y


def points_along(path: list[Point], distances: list[float]) -> list[Point]:
    """Points at the given distances plus every corner of ``path``, in path order.

    Used to place way nodes at chosen positions while keeping the way on the
    path's geometry.
    """
    cum = cumulative_m(path)
    wanted = sorted({round(d, 9) for d in list(distances) + cum if 0.0 <= d <= cum[-1]})
    return [point_along(path, d) for d in wanted]


def _locate(path: list[Point], cum: list[float], d: float) -> tuple[float, float, float, float]:
    """Point at distance d plus the unit direction of the edge it lies on."""
    d = min(max(d, 0.0), cum[-1])
    i = 0
    while i < len(cum) - 2 and cum[i + 1] < d:
        i += 1
    a, b = path[i], path[i + 1]
    edge = cum[i + 1] - cum[i]
    t = 0.0 if edge == 0 else (d - cum[i]) / edge
    ux, uy = ((b[0] - a[0]) / edge, (b[1] - a[1]) / edge) if edge else (1.0, 0.0)
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, ux, uy


def _left_normal(path: list[Point], cum: list[float], d: float) -> Point:
    """Unit normal to the left of travel at distance d, averaged at corners."""
    normals: list[Point] = []
    for i in range(len(path) - 1):
        lo, hi = cum[i], cum[i + 1]
        if hi == lo:
            continue
        if lo - 1e-9 <= d <= hi + 1e-9:
            ux = (path[i + 1][0] - path[i][0]) / (hi - lo)
            uy = (path[i + 1][1] - path[i][1]) / (hi - lo)
            normals.append((-uy, ux))
    nx = sum(n[0] for n in normals)
    ny = sum(n[1] for n in normals)
    norm = math.hypot(nx, ny) or 1.0
    return nx / norm, ny / norm


# ---------------------------------------------------------------------------
# ITRS export


def _sample_distances(cum: list[float], mandatory: list[float], spacing_m: float) -> list[float]:
    """Every ``spacing_m`` along the path, plus the mandatory distances.

    Regular samples closer than a quarter spacing to a mandatory one are
    dropped, so no near-zero edges appear next to a boundary or corner.
    """
    total = cum[-1]
    keep = sorted(set(mandatory) | set(cum) | {0.0, total})
    regular = []
    k = 1
    while k * spacing_m < total:
        d = k * spacing_m
        if all(abs(d - m) >= spacing_m / 4 for m in keep):
            regular.append(d)
        k += 1
    return sorted(set(keep) | set(regular))


def itrs_export_along(
    network: Network,
    path_m: list[Point],
    segments: list[dict[str, Any]],
    *,
    spacing_m: float = 8.0,
    offset_m: float = 0.0,
    jitter_m: float = 0.0,
    seed: int = 1,
    track_name: str = "Test Trail",
    style: str = "5.x",
    points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An ITRS-shaped GeoJSON export of a rater walking ``path_m``.

    ``segments`` run back to back along the path; each entry is
    ``{"length_m", "tech", "exposure"}`` plus optional ``track_type``,
    ``use``, ``direction`` and ``name``. Every segment boundary is a vertex
    shared by both segments. ``offset_m`` shifts the track to the left of
    travel (negative: right); ``jitter_m`` adds uniform perpendicular noise
    in [-jitter_m, +jitter_m] per vertex, seeded by ``seed``.

    ``points`` are rating points, ``{"type": "Exposure", "at_m": 120.0,
    "props": {...}}``. Each sits on the track vertex nearest ``at_m`` and
    references the segment holding that vertex.
    """
    if not segments:
        raise ValueError("need at least one segment")
    if spacing_m <= 0:
        raise ValueError("spacing_m must be positive")
    cum = cumulative_m(path_m)
    total = cum[-1]

    bounds = [0.0]
    for index, seg in enumerate(segments, start=1):
        if bounds[-1] >= total - 1e-9:
            raise ValueError(f"segment {index} starts at or past the end of the path")
        bounds.append(min(bounds[-1] + float(seg["length_m"]), total))

    distances = _sample_distances(cum, bounds, spacing_m)
    rng = random.Random(seed)
    coords: list[list[float]] = []
    for d in distances:
        x, y, _ux, _uy = _locate(path_m, cum, d)
        nx, ny = _left_normal(path_m, cum, d)
        shift = offset_m + (rng.uniform(-jitter_m, jitter_m) if jitter_m else 0.0)
        lon, lat = network.to_lonlat(x + nx * shift, y + ny * shift)
        coords.append([round(lon, 8), round(lat, 8), round(300.0 + 0.03 * d, 1)])

    index_of = {d: i for i, d in enumerate(distances)}
    features: list[dict[str, Any]] = []
    lengths: list[float] = []
    for position, seg in enumerate(segments, start=1):
        first, last = index_of[bounds[position - 1]], index_of[bounds[position]]
        seg_coords = [list(c) for c in coords[first : last + 1]]
        length = sum(distance_m(a, b) for a, b in zip(seg_coords, seg_coords[1:]))
        lengths.append(length)
        features.append(
            segment_feature(
                seg.get("name", f"S{position:03d}"),
                position,
                seg_coords,
                tech=seg["tech"],
                exposure=seg["exposure"],
                style=style,
                track_name=track_name,
                track_type=seg.get("track_type", TRACK_TYPE_SINGLE),
                use=seg.get("use", "Shared use"),
                direction=seg.get("direction", "Bi-directional"),
                extra={"segment_length_m": round(length)},
            )
        )

    point_features: list[dict[str, Any]] = []
    for spec in points or []:
        at = min(max(float(spec["at_m"]), 0.0), total)
        vertex = min(range(len(distances)), key=lambda i: abs(distances[i] - at))
        seg_pos = len(segments)
        for position in range(1, len(segments) + 1):
            if index_of[bounds[position - 1]] <= vertex < index_of[bounds[position]]:
                seg_pos = position
                break
        seg_props = features[seg_pos - 1]["properties"]
        point_features.append(
            point_feature(
                spec["type"],
                list(coords[vertex]),
                seg_props["segment_uuid"],
                point_segment_name=seg_props["segment_name"],
                point_track_name=track_name,
                **spec.get("props", {}),
            )
        )

    export = make_export(features, point_features, style=style)
    start_props = export["features"][0]["properties"]
    total_len = sum(lengths) or 1.0
    start_props.update(
        {
            "point_start_track_name": track_name,
            "point_start_track_length_m": round(sum(lengths)),
            "point_start_track_max_tech_rating": max(int(s["tech"]) for s in segments),
            "point_start_track_max_exposure_rating": max(int(s["exposure"]) for s in segments),
        }
    )
    for level in range(6):
        share = sum(length for length, seg in zip(lengths, segments) if int(seg["tech"]) == level)
        start_props[f"point_start_track_share_techLevel_{level}_perc"] = round(
            100.0 * share / total_len, 1
        )
    return export


# ---------------------------------------------------------------------------
# Scenarios
#
# Every scenario returns (network_xml, export, expect). ``expect`` always has
# these keys; distances are local meters along a way from its first node:
#
#   name, mimics          scenario name and the real-world case it copies
#   origin                (lon, lat) of local (0, 0)
#   ways, relations       {role: id} for the elements a test needs to find
#   trail_way_ids         ways that should receive ITRS tags, in track order
#   untouched_way_ids     ways that must not be split or get ITRS tags
#   cuts                  {way_id: [{"at_m", "kind", "node_id"}]} for every
#                         trail way; kind is "insert" (new node on the way),
#                         "snap" (existing node within 5 m, node_id set) or
#                         "junction"; an empty list means no split
#   pieces                {way_id: [{"from_m", "to_m", "tags"}]} the final
#                         ITRS tags along each trail way (only the two ITRS
#                         keys; {} where none)
#   way_nodes_m           {way_id: [(node_id, at_m)]} for every way
#   unmatched_segments    segment names reported as unmatched
#   not_applied           {segment name: reason} matched but deliberately
#                         not tagged
#   not_rated             [{"segment", "key"}] values of 0 that write no tag
#   dropped_tails         [{"way_id", "length_m"}] slivers below --min-piece
#   refused               [{"way_id", "key", "old", "new"}] without --force
#   parse_warnings        how many warnings parse_track should report
#
# Exposure points are built with ``exposure_point`` (realistic ITRS props).
# Scenario-specific extras are documented on each function.

PATH_TAGS = {"highway": "path", "bicycle": "designated", "mtb:scale:imba": "1"}


def _tags(tech: int, exposure: int) -> dict[str, str]:
    out = {}
    if tech:
        out[TECH] = str(tech)
    if exposure:
        out[EXPOSURE] = str(exposure)
    return out


def _piece(from_m: float, to_m: float, tech: int = 0, exposure: int = 0) -> dict[str, Any]:
    return {"from_m": from_m, "to_m": to_m, "tags": _tags(tech, exposure)}


def _seg(length_m: float, tech: int, exposure: int, **extra: Any) -> dict[str, Any]:
    return {"length_m": length_m, "tech": tech, "exposure": exposure, **extra}


EXPOSURE_LENGTHS = ("up to 5m long", "up to 30m long", "longer than 30m")


def exposure_point(
    at_m: float,
    rating: int,
    *,
    length: str | None = None,
    width_cm: int = 35,
    off_camber_deg: float = 10.0,
    remark: str = "",
    photos: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A point spec for ``itrs_export_along``: an ITRS Exposure point at ``at_m``.

    The props mirror a real export: the rating is an int, width and
    off-camber are strings, and the length is a text bucket that ITRS leaves
    blank for rating 2 (``length`` defaults to "up to 30m long" otherwise).
    The segment and track references are filled in by ``itrs_export_along``.
    """
    if length is None:
        length = "" if rating <= 2 else EXPOSURE_LENGTHS[1]
    props: dict[str, Any] = {
        "point_exposure_rating": rating,
        "point_exposure_width_of_trail_bed_cm": str(width_cm),
        "point_exposure_off_camber_deg": f"{off_camber_deg:.1f}",
        "point_exposure_length": length,
        "point_remark": remark,
    }
    for k in range(3):
        props[f"point_photo_{k + 1}"] = photos[k] if k < len(photos) else ""
    return {"type": "Exposure", "at_m": float(at_m), "props": props}


def _nearest_node(net: Network, way_id: int, at_m: float) -> tuple[int, float]:
    """(node id, distance along the way) of the way node nearest ``at_m``."""
    return min(net.way_nodes_m(way_id), key=lambda pair: abs(pair[1] - at_m))


def _expect(net: Network, name: str, mimics: str, **fields: Any) -> dict[str, Any]:
    expect: dict[str, Any] = {
        "name": name,
        "mimics": mimics,
        "origin": (net.origin_lon, net.origin_lat),
        "ways": {},
        "relations": {},
        "trail_way_ids": [],
        "untouched_way_ids": [],
        "cuts": {},
        "pieces": {},
        "unmatched_segments": [],
        "not_applied": {},
        "not_rated": [],
        "dropped_tails": [],
        "refused": [],
        "parse_warnings": 0,
    }
    expect.update(fields)
    expect["way_nodes_m"] = {w: net.way_nodes_m(w) for w in net.way_ids}
    return expect


def single_way_uniform() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One 400 m path, one segment covering all of it: tag it, no split.

    Mimics the simplest real case, On the Edge with every segment rated the
    same: the whole way gets one rating.
    """
    net = Network()
    path = legs((0, 0), [(150, 80), (150, 100), (100, 70)])
    way = net.polyline(path, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=20)
    export = itrs_export_along(net, path, [_seg(400, 2, 1)], offset_m=2.0, jitter_m=1.5)
    expect = _expect(
        net,
        "single_way_uniform",
        "On the Edge, one rating end to end",
        ways={"trail": way},
        trail_way_ids=[way],
        cuts={way: []},
        pieces={way: [_piece(0, 400, 2, 1)]},
    )
    return net.to_xml(), export, expect


def single_way_three_segments() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One 600 m way, segments 200/200/200 rated (2,1), (2,1), (3,1).

    The first two share a rating, so they merge: one cut at 400 m (inserted,
    the nearest node is 15 m away), none at 200 m. Mimics Fish Cove and
    Visp, where adjacent segments often share a rating.
    """
    net = Network()
    path = legs((0, 0), [(250, 60), (350, 95)])
    nodes = points_along(path, [float(d) for d in range(0, 601, 35)] + [600.0])
    way = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail"})
    export = itrs_export_along(
        net, path, [_seg(200, 2, 1), _seg(200, 2, 1), _seg(200, 3, 1)], jitter_m=1.5
    )
    expect = _expect(
        net,
        "single_way_three_segments",
        "Fish Cove / Visp: adjacent equal ratings merged before cutting",
        ways={"trail": way},
        trail_way_ids=[way],
        cuts={way: [{"at_m": 400.0, "kind": "insert", "node_id": None}]},
        pieces={way: [_piece(0, 400, 2, 1), _piece(400, 600, 3, 1)]},
    )
    return net.to_xml(), export, expect


def boundary_near_existing_node() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One 500 m way with rating changes at 150 m and 350 m.

    A node sits at 153 m, 3 m from the first change, so that cut snaps to
    it. The nearest nodes to the second change are at 310 m and 390 m, so
    a node is inserted at 350 m. S002 also carries an Exposure point at
    250 m. Mimics the common case where boundaries fall 1 to 15 m from an
    existing node, or far from any.

    Extra: ``snap_node_id`` (the node at 153 m).
    """
    net = Network()
    path = legs((0, 0), [(500, 85)])
    nodes = points_along(path, [0, 50, 100, 153, 200, 250, 310, 390, 440, 500])
    way = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail"})
    snap_node, snap_at = _nearest_node(net, way, 153.0)
    assert abs(snap_at - 153.0) < 1e-6
    export = itrs_export_along(
        net,
        path,
        [_seg(150, 2, 1), _seg(200, 3, 2), _seg(150, 2, 1)],
        jitter_m=1.0,
        points=[exposure_point(250.0, 2, width_cm=40, off_camber_deg=8.0)],
    )
    expect = _expect(
        net,
        "boundary_near_existing_node",
        "Rating changes 1 to 15 m from a node (snap) or far from any (insert)",
        ways={"trail": way},
        trail_way_ids=[way],
        cuts={
            way: [
                {"at_m": 150.0, "kind": "snap", "node_id": snap_node},
                {"at_m": 350.0, "kind": "insert", "node_id": None},
            ]
        },
        pieces={way: [_piece(0, 150, 2, 1), _piece(150, 350, 3, 2), _piece(350, 500, 2, 1)]},
        snap_node_id=snap_node,
    )
    return net.to_xml(), export, expect


def track_spans_two_ways() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Two ways end to end sharing a junction node, one segment on each.

    The ratings differ, but the boundary is the shared node, so neither way
    is split. Mimics a rater's track running from one named trail onto the
    next at a junction.

    Extra: ``boundary_node`` (the shared junction node).
    """
    net = Network()
    path_a = legs((0, 0), [(300, 80)])
    path_b = legs(path_a[-1], [(250, 110)])
    way_a = net.polyline(path_a, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=25)
    junction = net.way_nodes(way_a)[-1]
    way_b = net.polyline([junction, path_b[-1]], {**PATH_TAGS, "name": "Test Trail"}, spacing_m=25)
    export = itrs_export_along(
        net, path_a + path_b[1:], [_seg(300, 2, 1), _seg(250, 3, 2)], jitter_m=1.5
    )
    expect = _expect(
        net,
        "track_spans_two_ways",
        "Track crossing a junction where the rating also changes",
        ways={"first": way_a, "second": way_b},
        trail_way_ids=[way_a, way_b],
        cuts={way_a: [], way_b: []},
        pieces={way_a: [_piece(0, 300, 2, 1)], way_b: [_piece(0, 250, 3, 2)]},
        boundary_node=junction,
    )
    return net.to_xml(), export, expect


def junction_with_parallel_trail() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """A main way and a parallel way 6 m off it for 150 m.

    The parallel way leaves the main way at a junction 100 m in, runs 6 m
    north of it from 115 m to 265 m, then diverges. The track follows the
    main way 3 m to the north (toward the parallel way) with jitter, so a
    per-vertex nearest-way match flickers between the two. Everything
    belongs on the main way, with one inserted cut at 250 m. Mimics Loam
    Rider's start (flipping between Thunderbolt and Loam Rider) and Fish
    Cove brushing a parallel ``highway=track``.

    Extra: ``junction_node``.
    """
    net = Network()
    main_path = legs((0, 0), [(500, 90)])
    nodes = points_along(main_path, [0, 40, 80, 100, 120, 160, 200, 240, 280, 320, 400, 500])
    main = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail"})
    junction = _nearest_node(net, main, 100.0)[0]
    parallel = net.polyline(
        [junction, (115, 6), (265, 6), (300, 60)],
        {**PATH_TAGS, "name": "Other Trail"},
        spacing_m=30,
    )
    export = itrs_export_along(
        net, main_path, [_seg(250, 2, 1), _seg(250, 3, 1)], offset_m=3.0, jitter_m=1.0, seed=7
    )
    expect = _expect(
        net,
        "junction_with_parallel_trail",
        "Loam Rider start / Fish Cove parallel track: continuity beats nearest way",
        ways={"main": main, "parallel": parallel},
        trail_way_ids=[main],
        untouched_way_ids=[parallel],
        cuts={main: [{"at_m": 250.0, "kind": "insert", "node_id": None}]},
        pieces={main: [_piece(0, 250, 2, 1), _piece(250, 500, 3, 1)]},
        junction_node=junction,
    )
    return net.to_xml(), export, expect


def tail_onto_other_way() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The track runs 12 m past the end of its way onto another trail.

    Way A (300 m) ends at a junction where way B turns off southeast. The
    last segment carries on 12 m along B. That tail is shorter than the
    15 m minimum piece, so it's dropped: B stays whole and untagged. A is
    cut once at 150 m. Mimics Loam Rider's last vertices running onto
    Flo'Rion (and Visp's onto a ``highway=track``).
    """
    net = Network()
    path_a = legs((0, 0), [(300, 90)])
    path_b = legs(path_a[-1], [(200, 150)])
    nodes = points_along(path_a, [0, 40, 80, 120, 180, 220, 260, 300])
    way_a = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail"})
    junction = net.way_nodes(way_a)[-1]
    way_b = net.polyline([junction, path_b[-1]], {**PATH_TAGS, "name": "Flo Trail"}, spacing_m=25)
    track = path_a + [point_along(path_b, 12.0)]
    export = itrs_export_along(net, track, [_seg(150, 2, 1), _seg(200, 3, 1)], jitter_m=1.0)
    expect = _expect(
        net,
        "tail_onto_other_way",
        "Loam Rider's tail onto Flo'Rion",
        ways={"trail": way_a, "other": way_b},
        trail_way_ids=[way_a],
        untouched_way_ids=[way_b],
        cuts={way_a: [{"at_m": 150.0, "kind": "insert", "node_id": None}]},
        pieces={way_a: [_piece(0, 150, 2, 1), _piece(150, 300, 3, 1)]},
        dropped_tails=[{"way_id": way_b, "length_m": 12.0}],
    )
    return net.to_xml(), export, expect


def way_in_route_relation() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """A 500 m way in a ``type=route route=mtb`` relation is split in two.

    The relation holds three end-to-end ways: before (200 m), the trail way
    (500 m), after (200 m). Only the relation carries the name "Test Trail",
    as is common in OSM. The track covers the trail way with segments of
    300 m (2,1) and 200 m (3,2), so a node is inserted at 300 m. The first
    piece has more nodes, so it keeps the original id; the new piece goes
    into the relation right after it.

    Extra: ``relation_members_after``, a list of {"way_id", "from_m",
    "to_m"} in member order; way_id None means the new piece (negative id),
    from_m/to_m are along the original trail way and None for the
    neighbors. ``keeps_id`` is the (from_m, to_m) piece that keeps the id.
    """
    net = Network()
    before_path = legs((-200, 0), [(200, 90)])
    trail_path = legs((0, 0), [(250, 90), (250, 70)])
    after_path = legs(trail_path[-1], [(200, 70)])
    before = net.polyline(before_path, dict(PATH_TAGS), spacing_m=40)
    trail_nodes = points_along(trail_path, [0, 40, 80, 120, 160, 200, 240, 280, 320, 400, 480, 500])
    start_node = net.way_nodes(before)[-1]
    trail_pts: list[Point | int] = [start_node, *trail_nodes[1:]]
    trail = net.polyline(trail_pts, dict(PATH_TAGS))
    after = net.polyline([net.way_nodes(trail)[-1], after_path[-1]], dict(PATH_TAGS), spacing_m=40)
    relation = net.relation(
        [("way", before, ""), ("way", trail, ""), ("way", after, "")],
        {"type": "route", "route": "mtb", "name": "Test Trail"},
    )
    export = itrs_export_along(net, trail_path, [_seg(300, 2, 1), _seg(200, 3, 2)], jitter_m=1.5)
    expect = _expect(
        net,
        "way_in_route_relation",
        "Trail name on a route relation; split pieces inserted in member order",
        ways={"before": before, "trail": trail, "after": after},
        relations={"route": relation},
        trail_way_ids=[trail],
        untouched_way_ids=[before, after],
        cuts={trail: [{"at_m": 300.0, "kind": "insert", "node_id": None}]},
        pieces={trail: [_piece(0, 300, 2, 1), _piece(300, 500, 3, 2)]},
        relation_members_after=[
            {"way_id": before, "from_m": None, "to_m": None},
            {"way_id": trail, "from_m": 0.0, "to_m": 300.0},
            {"way_id": None, "from_m": 300.0, "to_m": 500.0},
            {"way_id": after, "from_m": None, "to_m": None},
        ],
        keeps_id=(0.0, 300.0),
    )
    return net.to_xml(), export, expect


def segment_far_from_any_way() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The second segment follows a trail that isn't in OSM.

    S001 covers a 300 m way. S002 then heads 60 m north and runs 150 m east,
    parallel to a second mapped way but 60 m from it. S001 is applied; S002
    is reported as unmatched. Mimics a rater surveying an unmapped trail.
    """
    net = Network()
    path_a = legs((0, 0), [(300, 90)])
    way_a = net.polyline(path_a, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=30)
    junction = net.way_nodes(way_a)[-1]
    way_b = net.polyline([junction, (450, 0)], {**PATH_TAGS, "name": "Other Trail"}, spacing_m=30)
    track = path_a + legs(path_a[-1], [(60, 0), (150, 90)])[1:]
    export = itrs_export_along(net, track, [_seg(300, 2, 1), _seg(210, 3, 1)], jitter_m=1.0)
    expect = _expect(
        net,
        "segment_far_from_any_way",
        "Rater surveyed a trail missing from OSM",
        ways={"trail": way_a, "other": way_b},
        trail_way_ids=[way_a],
        untouched_way_ids=[way_b],
        cuts={way_a: []},
        pieces={way_a: [_piece(0, 300, 2, 1)]},
        unmatched_segments=["S002"],
    )
    return net.to_xml(), export, expect


def existing_itrs_tags() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """A way already tagged technical_difficulty=2, exposure=1; ITRS says (3, 1).

    Without --force the tool refuses: no split, no tag change, non-zero
    exit. Exposure already matches. With --force the technical difficulty
    becomes 3 and the old value is reported. No real way has ITRS tags yet,
    so this guards a second run over the same area.

    Extra: ``pieces_with_force`` (the result with --force; ``pieces`` is
    the result without it), ``exit_nonzero``.
    """
    net = Network()
    path = legs((0, 0), [(300, 90)])
    way = net.polyline(
        path,
        {**PATH_TAGS, "name": "Test Trail", TECH: "2", EXPOSURE: "1"},
        spacing_m=25,
    )
    export = itrs_export_along(net, path, [_seg(300, 3, 1)], jitter_m=1.0)
    expect = _expect(
        net,
        "existing_itrs_tags",
        "Re-running over ways that already carry ITRS tags",
        ways={"trail": way},
        trail_way_ids=[way],
        cuts={way: []},
        pieces={way: [_piece(0, 300, 2, 1)]},
        pieces_with_force={way: [_piece(0, 300, 3, 1)]},
        refused=[{"way_id": way, "key": TECH, "old": "2", "new": "3"}],
        exit_nonzero=True,
    )
    return net.to_xml(), export, expect


def paved_road_segment() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The middle segment runs 120 m along a residential road between two paths.

    Path A (250 m) meets the road at one junction, the track follows the
    road north for 120 m, and path B leaves from a second junction. S002 is
    "Paved/surfaced road": it's matched to the road but never tagged, and
    the road is not split. Mimics road links between trails, which ITRS
    rates but doesn't present.

    Extra: ``matched_way_ids`` {segment name: [way ids]} (S002 still lands
    on the road).
    """
    net = Network()
    j1 = net.node(0, 0)
    j2 = net.node(0, 120)
    road = net.polyline(
        [(0, -140), j1, j2, (0, 260)],
        {"highway": "residential", "name": "Test Street", "surface": "asphalt"},
        spacing_m=40,
    )
    path_a = net.polyline([(-250, 0), j1], {**PATH_TAGS, "name": "Test Trail"}, spacing_m=25)
    path_b = net.polyline([j2, (250, 120)], {**PATH_TAGS, "name": "Test Trail"}, spacing_m=25)
    track = [(-250.0, 0.0), (0.0, 0.0), (0.0, 120.0), (250.0, 120.0)]
    export = itrs_export_along(
        net,
        track,
        [_seg(250, 2, 1), _seg(120, 1, 1, track_type=TRACK_TYPE_PAVED), _seg(250, 3, 1)],
        jitter_m=1.0,
    )
    expect = _expect(
        net,
        "paved_road_segment",
        "Road link between two trails: matched, never tagged",
        ways={"path_a": path_a, "road": road, "path_b": path_b},
        trail_way_ids=[path_a, path_b],
        untouched_way_ids=[road],
        cuts={path_a: [], path_b: []},
        pieces={path_a: [_piece(0, 250, 2, 1)], path_b: [_piece(0, 250, 3, 1)]},
        not_applied={"S002": "paved road"},
        matched_way_ids={"S001": [path_a], "S002": [road], "S003": [path_b]},
    )
    return net.to_xml(), export, expect


def not_rated_zero() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One segment rated technical difficulty 3, exposure 0 (Not rated).

    Only ``mtb:scale:itrs:technical_difficulty=3`` is written; no exposure
    tag. Mimics Loam Rider, where every segment has exposure 0.
    """
    net = Network()
    path = legs((0, 0), [(150, 100), (150, 80)])
    way = net.polyline(path, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=25)
    export = itrs_export_along(net, path, [_seg(300, 3, 0)], jitter_m=1.0)
    expect = _expect(
        net,
        "not_rated_zero",
        "Loam Rider: exposure 0 means not rated",
        ways={"trail": way},
        trail_way_ids=[way],
        cuts={way: []},
        pieces={way: [_piece(0, 300, 3, 0)]},
        not_rated=[{"segment": "S001", "key": EXPOSURE}],
    )
    return net.to_xml(), export, expect


def exposure_points() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Exposure points on a 600 m way with a side path joining at 300 m.

    The main way bends slightly at 300 m, where a side path heads north from
    a junction node shared by both ways. S001 (0-450 m, rated 2/3) and S002
    (450-600 m, rated 3/2) split the main way with an inserted node at 450 m.
    Five Exposure points sit on track vertices:

    - a: rated 2 at 144 m, 3 m from a plain node at 141 m: that node is tagged.
    - b: rated 3 at 216 m, mid-way in the 60 m gap between 190 m and 250 m:
      a node is inserted.
    - c: rated 2 exactly at the junction (300 m): the junction is ambiguous,
      so a node is inserted ~1 m along the main way, between the junction
      and the node at 340 m.
    - d: rated 1 at 400 m: below the default --exposure-min 2, skipped.
    - e: rated 2 at 520 m, 3 m from the node at 517 m, which already carries
      exposure=3: refused without --force, overwritten with it. That node
      ends up on the new piece (450-600 m).

    Mimics ITRS exposure points on a rated trail crossing a junction.

    Extra: ``junction_node``, ``tagged_node_id`` (the node at 517 m with
    exposure=3 already), and ``exposure_nodes``: one dict per Exposure
    point, in export order, with ``label``, ``at_m``, ``rating``,
    ``status`` (without --force), ``status_with_force``, ``kind`` ("node",
    "inserted" or "" when nothing is placed), ``node_id`` (the existing
    node for kind "node", else None), ``between`` (the original node ids on
    either side of an inserted node, else None), ``old`` (the existing
    value on refusal), and ``not_node`` (a node that must stay untouched).
    ``points_nodes_added`` is how many nodes apply_points inserts.
    """
    net = Network()
    path = legs((0, 0), [(300, 90), (300, 80)])
    dists = [0, 45, 95, 141, 190, 250, 300, 340, 380, 440, 480, 517, 560, 600]
    pts = points_along(path, [float(d) for d in dists])
    assert len(pts) == len(dists)
    ids = {
        d: net.node(*xy, tags={EXPOSURE: "3"} if d == 517 else None) for d, xy in zip(dists, pts)
    }
    main = net.polyline([ids[d] for d in dists], {**PATH_TAGS, "name": "Test Trail"})
    junction = ids[300]
    side = net.polyline([junction, (300, 200)], {**PATH_TAGS, "name": "Side Trail"}, spacing_m=30)
    export = itrs_export_along(
        net,
        path,
        [_seg(450, 2, 3), _seg(150, 3, 2)],
        jitter_m=0.5,
        seed=5,
        points=[
            exposure_point(144, 2, width_cm=60, off_camber_deg=5.0),
            exposure_point(216, 3, width_cm=30, off_camber_deg=15.0, length="up to 5m long"),
            exposure_point(300, 2),
            exposure_point(400, 1, width_cm=80, off_camber_deg=0.0),
            exposure_point(
                520,
                2,
                remark="narrow ledge above the creek",
                photos=("https://example.invalid/ledge.jpg",),
            ),
        ],
    )
    exposure_nodes = [
        {"label": "a", "at_m": 144.0, "rating": 2, "status": "tagged", "kind": "node",
         "node_id": ids[141], "between": None},
        {"label": "b", "at_m": 216.0, "rating": 3, "status": "tagged", "kind": "inserted",
         "node_id": None, "between": (ids[190], ids[250])},
        {"label": "c", "at_m": 300.0, "rating": 2, "status": "tagged", "kind": "inserted",
         "node_id": None, "between": (junction, ids[340]), "not_node": junction},
        {"label": "d", "at_m": 400.0, "rating": 1, "status": "skipped", "kind": "",
         "node_id": None, "between": None},
        {"label": "e", "at_m": 520.0, "rating": 2, "status": "refused",
         "status_with_force": "overwritten", "kind": "node", "node_id": ids[517],
         "between": None, "old": "3"},
    ]  # fmt: skip
    for p in exposure_nodes:
        p.setdefault("status_with_force", p["status"])
        p.setdefault("old", None)
        p.setdefault("not_node", None)
    expect = _expect(
        net,
        "exposure_points",
        "ITRS Exposure points on a rated trail crossing a junction",
        ways={"main": main, "side": side},
        trail_way_ids=[main],
        untouched_way_ids=[side],
        cuts={main: [{"at_m": 450.0, "kind": "insert", "node_id": None}]},
        pieces={main: [_piece(0, 450, 2, 3), _piece(450, 600, 3, 2)]},
        junction_node=junction,
        tagged_node_id=ids[517],
        exposure_nodes=exposure_nodes,
        points_nodes_added=2,
    )
    return net.to_xml(), export, expect


SCENARIOS = {
    fn.__name__: fn
    for fn in (
        single_way_uniform,
        single_way_three_segments,
        boundary_near_existing_node,
        track_spans_two_ways,
        junction_with_parallel_trail,
        tail_onto_other_way,
        way_in_route_relation,
        segment_far_from_any_way,
        existing_itrs_tags,
        paved_road_segment,
        not_rated_zero,
        exposure_points,
    )
}
