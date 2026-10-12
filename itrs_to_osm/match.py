"""Match ITRS segments onto OSM ways.

The problem: a rater's GPS track runs a few meters off the OSM way, flickers
between ways at junctions and alongside parallel trails, and sometimes runs
a few meters onto a neighboring trail at either end. Per-vertex "nearest
way" gets all of that wrong (see PLAN.md, "What the samples showed").

The approach: a small Viterbi decode over the track's vertices. Each vertex
may sit on any candidate way within the corridor, or on nothing. Emission
cost grows with the offset, is reduced for ways whose name matches the
track, and is raised when the OSM highway class doesn't fit the ITRS track
type. Transition cost is free along the same way, cheap between ways that
share a node nearby (a real junction), and expensive otherwise (a parallel
trail). Chainage along a way may not jump, which stops the decode from
hopping between folds of a switchback.

The result is, per ITRS segment, an ordered list of intervals on ways in way
chainage (meters from the way's first node), which is what the cutting
stage consumes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geo import (
    XY,
    LocalProjection,
    Projection,
    cumulative,
    dist,
    name_similarity,
    normalize_name,
    percentile,
    project_point,
)
from .itrs import (
    TRACK_TYPE_PAVED,
    TRACK_TYPE_SINGLE,
    TRACK_TYPE_UNSURFACED,
    Segment,
    Track,
)
from .osm import OsmData, Way

# A position on a way as (edge index, fraction along that edge), 0 <= t < 1,
# except the very end of the way which is (last edge, 1.0). Comparable as
# tuples, and independent of any projection, so the cutting stage can merge
# intervals from tracks matched in different local frames.
WayPos = tuple[int, float]


def way_pos(index: int, t: float, edge_count: int) -> WayPos:
    if t >= 1.0 and index < edge_count - 1:
        return (index + 1, 0.0)
    return (index, t)


# Cost model. Units are "sigma-squared": an offset of sigma meters costs 1.
NAME_WEIGHT = 3.0  # a full name match is worth about sqrt(3)*sigma of offset
NAME_MIN_SIMILARITY = 0.2  # ignore trivial token overlaps
NONE_EXTRA = 2.0  # being on no way costs the corridor-edge offset plus this
SWITCH_CONNECTED = 1.0  # moving to a way that shares a nearby node
SWITCH_UNCONNECTED = 12.0  # moving to a way with no junction nearby
SWITCH_NONE = 3.0  # leaving or rejoining the network
JUMP_PENALTY = 15.0  # chainage jumping along the same way (switchback fold)
JUNCTION_REACH_M = 40.0  # a shared node this close to the vertex counts as "here"

PATH_LIKE = frozenset({"path", "cycleway", "footway", "bridleway", "steps"})
TRACK_LIKE = frozenset({"track"})
ROAD_LIKE = frozenset(
    {
        "residential",
        "service",
        "unclassified",
        "tertiary",
        "secondary",
        "primary",
        "living_street",
        "pedestrian",
        "road",
        "tertiary_link",
        "secondary_link",
        "primary_link",
    }
)


def class_penalty(track_type: str, highway: str) -> float:
    """How badly an OSM highway class fits an ITRS track type (0 = fits)."""
    if track_type == TRACK_TYPE_SINGLE:
        if highway in PATH_LIKE:
            return 0.0
        return 1.0 if highway in TRACK_LIKE else 2.0
    if track_type == TRACK_TYPE_UNSURFACED:
        if highway in TRACK_LIKE or highway == "unclassified":
            return 0.0
        return 1.0
    if track_type == TRACK_TYPE_PAVED:
        if highway in ROAD_LIKE or highway == "cycleway":
            return 0.0
        return 1.0 if highway in TRACK_LIKE else 2.0
    return 0.0 if highway in PATH_LIKE else 1.0


@dataclass
class MatchOptions:
    corridor_m: float = 25.0
    max_offset_m: float = 15.0
    min_piece_m: float = 15.0
    sigma_m: float = 5.0
    name_hint: str | None = None  # --name: only ways/relations with this name get the prior
    way_ids: set[int] | None = None  # --way-id: restrict candidates to these ways


@dataclass
class Interval:
    """A stretch of one OSM way covered by (part of) one ITRS segment."""

    way_id: int
    start: float  # chainage, m, start <= end (in the matcher's local projection; for reports)
    end: float
    start_pos: WayPos  # projection-independent: (edge index, fraction along that edge)
    end_pos: WayPos
    reversed: bool  # the track runs against the way's node order
    vertices: int
    mean_offset: float
    p90_offset: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class SegmentMatch:
    segment: Segment
    intervals: list[Interval] = field(default_factory=list)
    dropped: list[Interval] = field(default_factory=list)  # tails shorter than min piece
    total_vertices: int = 0
    unmatched_vertices: int = 0
    p90_offset: float = 0.0
    status: str = "unmatched"  # matched | partial | unmatched
    reasons: list[str] = field(default_factory=list)

    @property
    def way_ids(self) -> list[int]:
        seen: list[int] = []
        for iv in self.intervals:
            if iv.way_id not in seen:
                seen.append(iv.way_id)
        return seen


@dataclass
class TrackMatch:
    track: Track
    segments: list[SegmentMatch]
    name_matches: dict[int, tuple[float, str]]  # way id -> (similarity, where the name came from)
    candidate_count: int
    warnings: list[str] = field(default_factory=list)

    def intervals_by_way(self) -> dict[int, list[tuple[Segment, Interval]]]:
        out: dict[int, list[tuple[Segment, Interval]]] = {}
        for sm in self.segments:
            for iv in sm.intervals:
                out.setdefault(iv.way_id, []).append((sm.segment, iv))
        return out


@dataclass
class _Candidate:
    way: Way
    xy: list[XY]
    cum: list[float]
    bbox: tuple[float, float, float, float]
    highway: str
    name_bonus: float
    node_xy: dict[int, XY]
    node_index: dict[int, list[int]]  # node id -> vertex indices (closed ways: two)

    @property
    def edge_count(self) -> int:
        return len(self.xy) - 1

    def node_position(self, node_id: int, near: float) -> tuple[float, int] | None:
        """(chainage, vertex index) of the node occurrence nearest to `near`."""
        indices = self.node_index.get(node_id)
        if not indices:
            return None
        k = min(indices, key=lambda i: abs(self.cum[i] - near))
        return self.cum[k], k


def _way_names(way: Way, data: OsmData) -> list[tuple[str, str]]:
    """(name, where) for the way and its route relations."""
    names: list[tuple[str, str]] = []
    if way.tags.get("name"):
        names.append((way.tags["name"], f"way {way.id} name"))
    for rel in data.parent_relations(way):
        if rel.tags.get("type") == "route" and rel.tags.get("name"):
            names.append((rel.tags["name"], f"relation {rel.id} name"))
    return names


def _name_bonus(way: Way, data: OsmData, track: Track, options: MatchOptions) -> tuple[float, str]:
    best, where = 0.0, ""
    for name, source in _way_names(way, data):
        if options.name_hint:
            score = 1.0 if normalize_name(name) == normalize_name(options.name_hint) else 0.0
        else:
            score = name_similarity(track.name, name)
        if score > best:
            best, where = score, f"{source} {name!r}"
    if best < NAME_MIN_SIMILARITY:
        return 0.0, ""
    return best, where


def _build_candidates(
    track: Track, data: OsmData, proj: LocalProjection, options: MatchOptions
) -> tuple[list[_Candidate], dict[int, tuple[float, str]], list[str]]:
    candidates: list[_Candidate] = []
    names: dict[int, tuple[float, str]] = {}
    warnings: list[str] = []
    for way in data.ways.values():
        highway = way.tags.get("highway", "")
        if not highway or way.tags.get("area") == "yes":
            continue
        if options.way_ids is not None and way.id not in options.way_ids:
            continue
        try:
            lonlat = data.way_coords(way)
        except KeyError as e:
            warnings.append(f"way {way.id} skipped: node {e.args[0]} not downloaded")
            continue
        if len(lonlat) < 2:
            continue
        xy = [proj.to_xy(lon, lat) for lon, lat in lonlat]
        cum = cumulative(xy)
        xs = [p[0] for p in xy]
        ys = [p[1] for p in xy]
        c = options.corridor_m
        node_xy: dict[int, XY] = {}
        node_index: dict[int, list[int]] = {}
        for k, (nid, p) in enumerate(zip(way.nodes, xy)):
            node_xy[nid] = p
            node_index.setdefault(nid, []).append(k)
        bonus, where = _name_bonus(way, data, track, options)
        if bonus > 0.0:
            names[way.id] = (bonus, where)
        candidates.append(
            _Candidate(
                way=way,
                xy=xy,
                cum=cum,
                bbox=(min(xs) - c, min(ys) - c, max(xs) + c, max(ys) + c),
                highway=highway,
                name_bonus=bonus,
                node_xy=node_xy,
                node_index=node_index,
            )
        )
    return candidates, names, warnings


def _track_vertices(
    track: Track, proj: LocalProjection
) -> tuple[list[XY], list[tuple[int, int]], list[float]]:
    """Concatenate segment vertices, sharing the boundary vertex between
    neighbors. Returns (xy, per-segment inclusive index ranges, gap to next
    vertex per index)."""
    verts: list[XY] = []
    ranges: list[tuple[int, int]] = []
    for seg in track.segments:
        pts = [proj.to_xy(c[0], c[1]) for c in seg.coords]
        if verts and dist(verts[-1], pts[0]) < 0.05:
            start = len(verts) - 1
            verts.extend(pts[1:])
        else:
            start = len(verts)
            verts.extend(pts)
        ranges.append((start, len(verts) - 1))
    gaps = [dist(a, b) for a, b in zip(verts, verts[1:])] + [0.0]
    return verts, ranges, gaps


def _segment_for_vertex(ranges: list[tuple[int, int]], index: int) -> int:
    for j, (s, e) in enumerate(ranges):
        if s <= index <= e:
            return j
    return len(ranges) - 1


def _connected_near(a: _Candidate, b: _Candidate, at: XY, cache: dict) -> int | None:
    """A node shared by both ways within JUNCTION_REACH_M of `at`, or None."""
    key = (a.way.id, b.way.id)
    shared = cache.get(key)
    if shared is None:
        shared = [nid for nid in a.node_xy if nid in b.node_xy]
        cache[key] = shared
        cache[(b.way.id, a.way.id)] = shared
    best, best_d = None, JUNCTION_REACH_M
    for nid in shared:
        d = dist(a.node_xy[nid], at)
        if d <= best_d:
            best, best_d = nid, d
    return best


def _decode(
    verts: list[XY],
    ranges: list[tuple[int, int]],
    gaps: list[float],
    track: Track,
    candidates: list[_Candidate],
    options: MatchOptions,
) -> list[tuple[int | None, Projection | None]]:
    """Viterbi over vertices. State = candidate index, or None for off-network."""
    sigma2 = options.sigma_m**2
    none_cost = options.corridor_m**2 / sigma2 + NONE_EXTRA
    obs: list[list[tuple[int, Projection]]] = []
    for i, v in enumerate(verts):
        seg = track.segments[_segment_for_vertex(ranges, i)]
        here: list[tuple[int, Projection]] = []
        for ci, cand in enumerate(candidates):
            bx = cand.bbox
            if not (bx[0] <= v[0] <= bx[2] and bx[1] <= v[1] <= bx[3]):
                continue
            p = project_point(v, cand.xy, cand.cum)
            if p.distance <= options.corridor_m:
                here.append((ci, p))
        obs.append(here)
    del seg  # per-vertex emission uses the segment below

    def emission(i: int, ci: int, p: Projection) -> float:
        seg = track.segments[_segment_for_vertex(ranges, i)]
        cand = candidates[ci]
        return (
            p.distance**2 / sigma2
            + class_penalty(seg.track_type, cand.highway)
            - NAME_WEIGHT * cand.name_bonus
        )

    shared_cache: dict = {}
    # Each layer: list of states; state = (ci or None, projection or None); cost; backpointer.
    layers: list[list[tuple[int | None, Projection | None]]] = []
    costs: list[list[float]] = []
    back: list[list[int]] = []
    for i in range(len(verts)):
        states: list[tuple[int | None, Projection | None]] = [(ci, p) for ci, p in obs[i]]
        states.append((None, None))
        layer_cost: list[float] = []
        layer_back: list[int] = []
        for ci, p in states:
            e = none_cost if ci is None else emission(i, ci, p)  # type: ignore[arg-type]
            if i == 0:
                layer_cost.append(e)
                layer_back.append(-1)
                continue
            best, best_k = math.inf, -1
            for k, (pci, pp) in enumerate(layers[i - 1]):
                t: float
                if pci is None and ci is None:
                    t = 0.0
                elif pci is None or ci is None:
                    t = SWITCH_NONE
                elif pci == ci:
                    assert pp is not None and p is not None
                    allowed = 3.0 * gaps[i - 1] + 10.0
                    t = 0.0 if abs(p.along - pp.along) <= allowed else JUMP_PENALTY
                else:
                    node = _connected_near(
                        candidates[pci], candidates[ci], verts[i - 1], shared_cache
                    )
                    if node is None:
                        node = _connected_near(
                            candidates[pci], candidates[ci], verts[i], shared_cache
                        )
                    t = SWITCH_CONNECTED if node is not None else SWITCH_UNCONNECTED
                total = costs[i - 1][k] + t
                if total < best:
                    best, best_k = total, k
            layer_cost.append(best + e)
            layer_back.append(best_k)
        layers.append(states)
        costs.append(layer_cost)
        back.append(layer_back)

    # Backtrack from the cheapest final state.
    path: list[tuple[int | None, Projection | None]] = []
    k = min(range(len(costs[-1])), key=lambda j: costs[-1][j])
    for i in range(len(verts) - 1, -1, -1):
        path.append(layers[i][k])
        k = back[i][k]
    path.reverse()
    return path


def _intervals_for_segment(
    seg_index: int,
    ranges: list[tuple[int, int]],
    verts: list[XY],
    path: list[tuple[int | None, Projection | None]],
    candidates: list[_Candidate],
    shared_cache: dict,
) -> tuple[list[Interval], int]:
    """Group a segment's vertex assignments into way intervals.

    Returns (intervals in track order, count of unmatched vertices).
    """
    s, e = ranges[seg_index]
    groups: list[tuple[int, list[int]]] = []  # (candidate index, vertex indices)
    unmatched = 0
    for i in range(s, e + 1):
        ci, _ = path[i]
        if ci is None:
            unmatched += 1
            continue
        if groups and groups[-1][0] == ci:
            groups[-1][1].append(i)
        else:
            groups.append((ci, [i]))

    # A lone boundary vertex (shared with the neighboring segment) sitting on
    # the neighboring way at a junction is not a tail of this segment; the
    # adjacent interval extends to that junction instead.
    def junction_artifact(group: tuple[int, list[int]], neighbor: tuple[int, list[int]]) -> bool:
        if len(group[1]) != 1:
            return False
        at = verts[group[1][0]]
        return (
            _connected_near(candidates[group[0]], candidates[neighbor[0]], at, shared_cache)
            is not None
        )

    before: _Candidate | None = None
    after: _Candidate | None = None
    if len(groups) > 1 and seg_index > 0 and groups[0][1] == [s]:
        if junction_artifact(groups[0], groups[1]):
            before = candidates[groups.pop(0)[0]]
    if len(groups) > 1 and seg_index < len(ranges) - 1 and groups[-1][1] == [e]:
        if junction_artifact(groups[-1], groups[-2]):
            after = candidates[groups.pop()[0]]

    intervals: list[Interval] = []
    for g, (ci, idxs) in enumerate(groups):
        cand = candidates[ci]
        projs = [path[i][1] for i in idxs]
        assert all(p is not None for p in projs)
        offsets = [p.distance for p in projs]  # type: ignore[union-attr]
        first, last = projs[0], projs[-1]
        assert first is not None and last is not None
        reverse = last.along < first.along
        lo, hi = (last, first) if reverse else (first, last)
        a0, a1 = lo.along, hi.along
        p0 = way_pos(lo.index, lo.t, cand.edge_count)
        p1 = way_pos(hi.index, hi.t, cand.edge_count)

        # Extend to the shared junction node when the track arrived from, or
        # leaves onto, a connected way, so neighboring intervals meet exactly.
        def extend(
            cand: _Candidate, other: _Candidate, at_vertex: XY, near: float, toward_start: bool
        ) -> None:
            nonlocal a0, a1, p0, p1
            node = _connected_near(other, cand, at_vertex, shared_cache)
            if node is None:
                return
            found = cand.node_position(node, near)
            if found is None:
                return
            along, k = found
            # The junction must be close along the way too, or a shared node
            # far down a curving way would stretch a short tail into a long one.
            if abs(along - near) > JUNCTION_REACH_M:
                return
            if toward_start and along < a0:
                a0, p0 = along, (k, 0.0)
            elif not toward_start and along > a1:
                a1, p1 = along, way_pos(k, 0.0, cand.edge_count)

        prev = candidates[groups[g - 1][0]] if g > 0 else before
        if prev is not None:
            extend(cand, prev, verts[idxs[0]], first.along, toward_start=not reverse)
        nxt = candidates[groups[g + 1][0]] if g < len(groups) - 1 else after
        if nxt is not None:
            extend(cand, nxt, verts[idxs[-1]], last.along, toward_start=reverse)

        if a1 - a0 < 1.0 and len(groups) > 1:
            continue
        intervals.append(
            Interval(
                way_id=cand.way.id,
                start=a0,
                end=a1,
                start_pos=p0,
                end_pos=p1,
                reversed=reverse,
                vertices=len(idxs),
                mean_offset=sum(offsets) / len(offsets),
                p90_offset=percentile(offsets, 90),
            )
        )
    return intervals, unmatched


def match_track(track: Track, data: OsmData, options: MatchOptions | None = None) -> TrackMatch:
    """Match every segment of the track onto the downloaded ways."""
    options = options or MatchOptions()
    proj = LocalProjection.for_bbox(track.bbox())
    candidates, names, warnings = _build_candidates(track, data, proj, options)
    verts, ranges, gaps = _track_vertices(track, proj)
    if not candidates:
        warnings.append("no candidate highway ways in the downloaded area")
        path: list[tuple[int | None, Projection | None]] = [(None, None)] * len(verts)
    else:
        path = _decode(verts, ranges, gaps, track, candidates, options)

    shared_cache: dict = {}
    results: list[SegmentMatch] = []
    for j, seg in enumerate(track.segments):
        s, e = ranges[j]
        sm = SegmentMatch(segment=seg, total_vertices=e - s + 1)
        intervals, sm.unmatched_vertices = _intervals_for_segment(
            j, ranges, verts, path, candidates, shared_cache
        )
        offsets = [path[i][1].distance for i in range(s, e + 1) if path[i][1] is not None]  # type: ignore[union-attr]
        sm.p90_offset = percentile(offsets, 90)

        if not intervals:
            sm.status = "unmatched"
            sm.reasons.append("no way within the corridor")
            results.append(sm)
            continue
        if sm.p90_offset > options.max_offset_m:
            sm.status = "unmatched"
            sm.reasons.append(
                f"90th percentile offset {sm.p90_offset:.1f} m exceeds {options.max_offset_m:.0f} m"
            )
            results.append(sm)
            continue

        # The longest interval always stays. Shorter secondary ones are tails
        # or crossing artifacts when they're under the minimum piece length or
        # sit badly off their way.
        longest = max(intervals, key=lambda iv: iv.length)
        for iv in intervals:
            if iv is longest:
                sm.intervals.append(iv)
            elif iv.length < options.min_piece_m:
                sm.dropped.append(iv)
                sm.reasons.append(
                    f"{iv.length:.0f} m on way {iv.way_id} dropped (shorter than "
                    f"{options.min_piece_m:.0f} m)"
                )
            elif iv.p90_offset > options.max_offset_m:
                sm.dropped.append(iv)
                sm.reasons.append(
                    f"{iv.length:.0f} m on way {iv.way_id} dropped (90th percentile offset "
                    f"{iv.p90_offset:.1f} m exceeds {options.max_offset_m:.0f} m)"
                )
            else:
                sm.intervals.append(iv)
        if sm.unmatched_vertices:
            sm.reasons.append(
                f"{sm.unmatched_vertices} of {sm.total_vertices} vertices off-network"
            )
        sm.status = "partial" if (sm.dropped or sm.unmatched_vertices) else "matched"
        results.append(sm)

    return TrackMatch(
        track=track,
        segments=results,
        name_matches=names,
        candidate_count=len(candidates),
        warnings=warnings,
    )
