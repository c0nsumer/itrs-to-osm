"""Put ITRS Exposure points on the map as tagged nodes.

ITRS rates a segment's exposure as that of its most exposed section, and
exports a point for each such section rated blue or worse. The way keeps the
segment's rating (`split.py`); this module adds ``mtb:scale:itrs:exposure``
to a node of the way at each point, so a renderer can call the spot out.

Placement comes from the match. Every point sits on a vertex of its
segment, and the decode already put that vertex on a way at a chainage
(``SegmentMatch.vertex_places``). After the ways are split, the node goes
on the piece holding that chainage: an existing node within ``snap_m``
unless it's a junction or carries other tags, else a new node inserted on
the way's own geometry. A point on a spot that another track from a newer
survey also claims is stale and gets no node: the newer survey exported its
own points there, and it may well have re-rated this one.

Reruns matter: once the ways are split at the segment boundaries, a short
segment can match as a dropped tail every time. So a point whose vertex sits
on a tail dropped for length still gets its node there, as long as the tail
is the same trail (a way named like one the track kept) rather than a
junction overshoot onto a neighbor. And a point the match can't place at all
is looked up by location: an existing exposure node within ``snap_m`` of the
surveyed spot counts as the point's node (already tagged, or refused when
the value differs).

A re-survey can also drop a point. ITRS exports a point for every exposed
section rated blue or worse, so a node carrying the tag on a stretch this run
surveyed, with no point of this run behind it, is stale: the tag comes off
(the node stays, it's part of the way's shape). Same date rule as a differing
value: removed when the survey is newer than the node's last OSM edit, else
refused unless forced. Nothing is removed where the segment's exposure isn't
rated, where one of the segment's own points couldn't be placed, on a
junction with a way outside the run, or outside the surveyed stretch.

Runs after ``split.apply_matches``; nothing here talks to OSM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .geo import LocalProjection, normalize_name, project_point
from .itrs import NOT_RATED, RatingPoint, Segment, distance_m
from .match import SegmentMatch, TrackMatch
from .osm import Node, OsmData, Way
from .split import (
    ApplyResult,
    Claim,
    Piece,
    WayChange,
    _chainage,
    _is_junction,
    _label,
    insert_node_at,
    sources_text,
    survey_day,
    survey_newer_than_edit,
)
from .tagging import EXPOSURE_KEY, EXPOSURE_VALUES

EXPOSURE_TYPE = "Exposure"
ON_VERTEX_TOL_M = 10.0  # a point further than this from every vertex is projected instead
MIN_EDGE_M = 1.0  # never insert a node closer than this to an existing one
INTERVAL_TOL_M = 1.0


@dataclass
class PointOptions:
    snap_m: float = 5.0
    min_exposure: int = 2  # ITRS exports blue (2) or worse, so the default writes them all
    max_offset_m: float = 15.0
    force: bool = False
    enabled: bool = True
    remove_stale: bool = True  # untag nodes the survey no longer has a point for


@dataclass
class PointChange:
    """What happened to one Exposure point."""

    track_name: str
    segment_name: str
    point: RatingPoint
    value: int | None  # the exposure rating, when it parsed
    status: str  # tagged | already | refused | skipped | unplaced | overwritten
    reason: str = ""
    way_id: int | None = None  # the piece the node sits on, after splits
    node_id: int | None = None
    kind: str = ""  # node | inserted
    moved_m: float = 0.0  # along the way, from the matched position to the node used
    from_point_m: float = 0.0  # straight line from the surveyed coordinate to the node
    offset_m: float = 0.0  # the point's distance from the way, per the match
    old: str | None = None  # the existing value when refused or overwritten
    surveyed: str = ""  # the segment's survey timestamp, "" when unknown
    source: str = ""  # "Track / S003" label of the point's segment, "" when unknown
    backs_node_id: int | None = None  # for a point not written: the node already marking it

    @property
    def written(self) -> bool:
        return self.status in ("tagged", "overwritten")


@dataclass
class StaleNode:
    """An existing exposure node on a surveyed stretch with no point behind it."""

    node_id: int
    way_id: int  # the piece the node sits on, after splits
    old: str  # the value the node carried
    status: str  # removed | refused | kept
    reason: str
    sources: list[str]  # "Track / S003" labels of the survey covering the spot
    surveyed: str
    lat: float
    lon: float


@dataclass
class PointsResult:
    changes: list[PointChange] = field(default_factory=list)
    nodes_added: int = 0
    stale: list[StaleNode] = field(default_factory=list)

    @property
    def refusals(self) -> list[PointChange]:
        return [c for c in self.changes if c.status == "refused"]

    @property
    def removed(self) -> list[StaleNode]:
        return [s for s in self.stale if s.status == "removed"]

    @property
    def stale_refusals(self) -> list[StaleNode]:
        return [s for s in self.stale if s.status == "refused"]

    @property
    def written(self) -> list[PointChange]:
        return [c for c in self.changes if c.written]


def _rating(point: RatingPoint) -> tuple[int | None, str]:
    raw = point.props.get("point_exposure_rating")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"exposure rating {raw!r} is not a number"
    if value == NOT_RATED:
        return value, "not rated (0)"
    if value not in EXPOSURE_VALUES:
        return value, f"exposure rating {value} outside 1-4"
    return value, ""


def _segment_match(tm: TrackMatch, point: RatingPoint) -> tuple[Segment, SegmentMatch] | None:
    for sm in tm.segments:
        seg = sm.segment
        if (point.segment_uuid and seg.uuid == point.segment_uuid) or (
            not point.segment_uuid and point.segment_name and seg.name == point.segment_name
        ):
            return seg, sm
    return None


def _kept_names(tm: TrackMatch, data: OsmData) -> set[str]:
    """Normalized names of every way the track's segments kept a stretch of."""
    names: set[str] = set()
    for sm in tm.segments:
        for iv in sm.intervals:
            way = data.ways.get(iv.way_id)
            if way is not None and way.tags.get("name"):
                names.add(normalize_name(way.tags["name"]))
    return names


def _locate(
    tm: TrackMatch, sm: SegmentMatch, point: RatingPoint, data: OsmData, options: PointOptions
) -> tuple[int, float, float] | str:
    """(way id, chainage m, offset m) for the point, or the reason it has none."""
    track = tm.track
    seg = sm.segment
    lonlat = (point.lon, point.lat)
    k, d = min(((i, distance_m(lonlat, c)) for i, c in enumerate(seg.coords)), key=lambda x: x[1])
    if d <= ON_VERTEX_TOL_M and k < len(sm.vertex_places):
        place = sm.vertex_places[k]
        if place is None:
            return "its track vertex is off-network"
        # A GPS backtrack can put the vertex a few meters outside the segment's
        # interval on the right way; clamp it in, within the snap distance.
        tol = max(INTERVAL_TOL_M, options.snap_m)
        for iv in sm.intervals:
            if iv.way_id == place.way_id and iv.start - tol <= place.along_m <= iv.end + tol:
                return place.way_id, min(max(place.along_m, iv.start), iv.end), place.offset_m
        tail = next((iv for iv in sm.dropped if iv.way_id == place.way_id), None)
        if tail is not None:
            # A tail dropped for length onto the same trail (a rerun over
            # ways already split at the segment boundaries) is still the
            # right spot for a node. A tail onto a differently named way is
            # a junction overshoot, and the node would land on the neighbor.
            way = data.ways.get(place.way_id)
            name = normalize_name(way.tags.get("name", "")) if way else ""
            if tail.p90_offset > options.max_offset_m or place.offset_m > options.max_offset_m:
                return (
                    f"it sits on the dropped tail onto way {place.way_id}, "
                    f"{tail.p90_offset:.0f} m off it"
                )
            if name and name in _kept_names(tm, data):
                return place.way_id, place.along_m, place.offset_m
            return f"it sits on the dropped tail onto way {place.way_id}, a different trail"
        return f"its track vertex landed outside the segment's stretch of way {place.way_id}"

    # Not on a vertex: project onto the segment's kept stretches instead.
    proj = LocalProjection.for_bbox(track.bbox())
    best: tuple[float, int, float] | None = None
    for iv in sm.intervals:
        way = data.ways.get(iv.way_id)
        if way is None:
            continue
        xy = [proj.to_xy(lon, lat) for lon, lat in data.way_coords(way)]
        p = project_point(proj.to_xy(*lonlat), xy)
        if iv.start - INTERVAL_TOL_M <= p.along <= iv.end + INTERVAL_TOL_M and (
            best is None or p.distance < best[0]
        ):
            best = (p.distance, iv.way_id, p.along)
    if best is None:
        return f"{d:.0f} m from the nearest vertex of {seg.name} and off its matched ways"
    if best[0] > options.max_offset_m:
        return f"{best[0]:.0f} m from the matched way (over {options.max_offset_m:.0f} m)"
    return best[1], best[2], best[0]


def _newer_claim(change: WayChange | None, track_name: str, along_m: float, surveyed: str):
    """Another track's claim from a newer survey covering ``along_m``, if any."""
    if change is None:
        return None
    newer = [
        c
        for c in change.claims
        if c.track_name != track_name
        and c.surveyed > surveyed
        and c.start_m - INTERVAL_TOL_M <= along_m <= c.end_m + INTERVAL_TOL_M
    ]
    return max(newer, key=lambda c: c.surveyed) if newer else None


def _piece_for(result: ApplyResult, way_id: int, along_m: float) -> tuple[int, float]:
    """(piece way id, chainage on that piece) after the splits."""
    change = next((w for w in result.ways if w.way_id == way_id), None)
    if change is None or not change.pieces:
        return way_id, along_m
    best: Piece | None = None
    best_d = float("inf")
    for p in change.pieces:
        if p.start_m <= along_m <= p.end_m:
            d = 0.0
        else:
            d = min(abs(along_m - p.start_m), abs(along_m - p.end_m))
        if d < best_d:
            best, best_d = p, d
    assert best is not None
    return best.way_id, min(max(along_m - best.start_m, 0.0), best.length)


def _usable(data: OsmData, node: Node) -> bool:
    """An existing node the tag may go on: not a junction, nothing else on it."""
    if _is_junction(data, node.id):
        return False
    return all(k == EXPOSURE_KEY for k in node.tags)


def _nearby_exposure_node(
    data: OsmData, point: RatingPoint, options: PointOptions
) -> tuple[Node, float] | None:
    """An existing exposure node within ``snap_m`` of the surveyed point."""
    best: tuple[Node, float] | None = None
    for node in data.nodes.values():
        if EXPOSURE_KEY not in node.tags:
            continue
        d = distance_m((point.lon, point.lat), (node.lon, node.lat))
        if d <= options.snap_m and (best is None or d < best[1]):
            best = (node, d)
    return best


def _choose_node(
    data: OsmData, way: Way, along_m: float, options: PointOptions
) -> tuple[Node, str, float]:
    """The node for a point at ``along_m`` on ``way``: existing or inserted."""
    cum = _chainage(data, way)
    best_k: int | None = None
    best_d = options.snap_m
    nearest_d = float("inf")
    nearest_k = 0
    for k, node_id in enumerate(way.nodes):
        d = abs(cum[k] - along_m)
        if d < nearest_d:
            nearest_d, nearest_k = d, k
        if d <= best_d and _usable(data, data.nodes[node_id]):
            best_k, best_d = k, d
    if best_k is not None:
        return data.nodes[way.nodes[best_k]], "node", best_d

    # Insert, keeping at least MIN_EDGE_M from the excluded neighbor so the
    # way gets no zero-length edge.
    target = along_m
    if nearest_d < MIN_EDGE_M:
        at = cum[nearest_k]
        target = at + MIN_EDGE_M if at + MIN_EDGE_M <= cum[-1] else at - MIN_EDGE_M
        target = min(max(target, 0.0), cum[-1])
    node, _ = insert_node_at(data, way, cum, target)
    data.mark_modified(way)
    return node, "inserted", abs(target - along_m)


def _existing_node_for(
    tm: TrackMatch, point: RatingPoint, data: OsmData, result: ApplyResult, options: PointOptions
) -> int | None:
    """The exposure node already marking a point this run won't write.

    A point skipped for its rating (below ``--exposure-min``, not rated)
    still says the spot is exposed, so its node isn't stale. Same placement
    as a written point, minus the writing: the tagged node within ``snap_m``
    of the matched position, else one within ``snap_m`` of the surveyed spot.
    """
    found = _segment_match(tm, point)
    if found is not None:
        sm = found[1]
        if sm.status != "unmatched" and (sm.intervals or sm.dropped):
            located = _locate(tm, sm, point, data, options)
            if not isinstance(located, str):
                piece_id, local_m = _piece_for(result, located[0], located[1])
                way = data.ways.get(piece_id)
                if way is not None:
                    cum = _chainage(data, way)
                    near = [
                        (abs(cum[k] - local_m), node_id)
                        for k, node_id in enumerate(way.nodes)
                        if EXPOSURE_KEY in data.nodes[node_id].tags
                        and abs(cum[k] - local_m) <= options.snap_m
                    ]
                    if near:
                        return min(near)[1]
    nearby = _nearby_exposure_node(data, point, options)
    return nearby[0].id if nearby else None


def _remove_stale(
    data: OsmData, result: ApplyResult, out: PointsResult, options: PointOptions
) -> None:
    """Untag exposure nodes on surveyed stretches that no point of this run backs."""
    supported = {n for c in out.changes for n in (c.node_id, c.backs_node_id) if n is not None}
    # A point that couldn't be placed may be the one behind a node, so its
    # segment's stretches are left alone (the whole track's, when the point
    # names no known segment).
    unplaced = [c for c in out.changes if c.status == "unplaced"]
    guarded_labels = {c.source for c in unplaced if c.source}
    guarded_tracks = {c.track_name for c in unplaced if not c.source}

    seen: set[int] = set()
    for change in result.ways:
        if not change.pieces or change.status == "refused":
            continue
        piece_ids = {p.way_id for p in change.pieces}
        for piece in change.pieces:
            way = data.ways.get(piece.way_id)
            if way is None:
                continue
            cum = _chainage(data, way)
            for k, node_id in enumerate(way.nodes):
                node = data.nodes[node_id]
                if EXPOSURE_KEY not in node.tags or node_id in supported or node_id in seen:
                    continue
                # Strictly inside a stretch whose survey rated exposure; a
                # node on the edge of the survey may belong to what's beyond.
                along_m = piece.start_m + cum[k]
                covering: list[Claim] = [
                    c
                    for c in change.claims
                    if EXPOSURE_KEY in c.tags and c.start_m < along_m < c.end_m
                ]
                if not covering:
                    continue
                if any(
                    w.id not in piece_ids
                    for w in data.parent_ways(node_id)
                    if w.tags.get("highway")
                ):
                    continue  # a junction with a way this survey may not cover
                seen.add(node_id)
                claim = max(covering, key=lambda c: c.surveyed)
                old = node.tags[EXPOSURE_KEY]
                stale = StaleNode(
                    node_id, way.id, old, "kept", "", list(claim.sources), claim.surveyed,
                    node.lat, node.lon,
                )  # fmt: skip
                out.stale.append(stale)
                held = [s for s in claim.sources if s in guarded_labels]
                if held or claim.track_name in guarded_tracks:
                    stale.reason = (
                        f"an Exposure point of {sources_text(held) or claim.track_name} "
                        "couldn't be placed, and it may be this node's"
                    )
                elif survey_newer_than_edit(claim.surveyed, node.timestamp):
                    stale.status = "removed"
                    stale.reason = (
                        f"the survey ({survey_day(claim.surveyed)}) is newer than the node's "
                        f"last OSM edit ({survey_day(node.timestamp or '')})"
                    )
                elif options.force:
                    stale.status, stale.reason = "removed", "forced (--force)"
                else:
                    stale.status = "refused"
                    stale.reason = (
                        f"the node's last OSM edit ({survey_day(node.timestamp or '') or '?'}) "
                        f"is not older than the survey ({survey_day(claim.surveyed) or '?'})"
                    )
                if stale.status == "removed":
                    del node.tags[EXPOSURE_KEY]
                    data.mark_modified(node)


def apply_points(
    data: OsmData,
    matches: list[TrackMatch],
    result: ApplyResult,
    options: PointOptions | None = None,
) -> PointsResult:
    """Tag a node for every Exposure point of every track, in place."""
    options = options or PointOptions()
    out = PointsResult()
    by_node: dict[int, PointChange] = {}  # node id -> the point that last decided its tag
    original: dict[int, str | None] = {}  # node id -> its value before this run

    for tm in matches:
        track = tm.track
        for point in track.points:
            if point.point_type != EXPOSURE_TYPE:
                continue
            value, problem = _rating(point)
            found = _segment_match(tm, point)
            seg_name = found[0].name if found else (point.segment_name or "?")
            change = PointChange(track.name, seg_name, point, value, "skipped")
            change.source = _label(track.name, found[0]) if found else ""
            out.changes.append(change)

            if not options.enabled:
                change.reason = "exposure nodes turned off (--no-exposure-nodes)"
                continue
            if problem:
                change.status = "skipped" if value == NOT_RATED else "unplaced"
                change.reason = problem
                if value == NOT_RATED:
                    change.backs_node_id = _existing_node_for(tm, point, data, result, options)
                continue
            assert value is not None
            if value < options.min_exposure:
                change.reason = f"rated {value}, below --exposure-min {options.min_exposure}"
                change.backs_node_id = _existing_node_for(tm, point, data, result, options)
                continue
            if found is None:
                change.status = "unplaced"
                change.reason = f"references unknown segment {point.segment_uuid or seg_name}"
                continue
            seg, sm = found
            change.surveyed = seg.last_mutated
            located: tuple[int, float, float] | str
            if sm.status == "unmatched":
                located = f"{seg.name} is unmatched"
            elif not sm.intervals and not sm.dropped:
                located = f"{seg.name} kept no stretch of any way"
            else:
                located = _locate(tm, sm, point, data, options)
            if isinstance(located, str):
                nearby = _nearby_exposure_node(data, point, options)
                if nearby is None:
                    change.status, change.reason = "unplaced", located
                    continue
                node, d = nearby
                parents = [w for w in data.parent_ways(node.id) if w.tags.get("highway")]
                change.node_id, change.kind = node.id, "node"
                change.way_id = parents[0].id if parents else None
                change.from_point_m = d
                existing = node.tags[EXPOSURE_KEY]
                where = f"found by location, {d:.1f} m from the surveyed point ({located})"
                if existing == str(value):
                    change.status, change.reason = "already", where
                elif options.force:
                    change.status, change.old = "overwritten", existing
                    change.reason = where
                    node.tags[EXPOSURE_KEY] = str(value)
                    data.mark_modified(node)
                else:
                    change.status, change.old = "refused", existing
                    change.reason = where
                continue
            way_id, along_m, change.offset_m = located
            if any(w.way_id == way_id and w.status == "refused" for w in result.ways):
                change.status = "skipped"
                change.reason = (
                    f"way {way_id} was refused (its existing ITRS value differs); "
                    "resolve that first, or rerun with --force"
                )
                continue
            way_change = next((w for w in result.ways if w.way_id == way_id), None)
            newer = _newer_claim(way_change, track.name, along_m, change.surveyed)
            if newer is not None:
                change.reason = (
                    f"stale: a newer survey ({survey_day(newer.surveyed)}, "
                    f"{sources_text(newer.sources)}) covers this spot on way {way_id}; "
                    "its own points apply"
                )
                continue
            piece_id, local_m = _piece_for(result, way_id, along_m)
            way = data.ways.get(piece_id)
            if way is None:
                change.status, change.reason = "unplaced", f"way {piece_id} is not in the data"
                continue

            node, change.kind, change.moved_m = _choose_node(data, way, local_m, options)
            change.way_id, change.node_id = way.id, node.id
            change.from_point_m = distance_m((point.lon, point.lat), (node.lon, node.lat))
            if change.kind == "inserted":
                out.nodes_added += 1
            new = str(value)
            # The node's value before this run touched it decides refusals.
            # When two points of this run land on one node, the newer survey
            # wins; between surveys of the same date, the higher rating stays.
            orig = original.setdefault(node.id, node.tags.get(EXPOSURE_KEY))
            earlier = by_node.get(node.id)
            current = node.tags.get(EXPOSURE_KEY)
            if earlier is not None and current is not None:
                older = change.surveyed < earlier.surveyed
                newer = change.surveyed > earlier.surveyed
                if older or (not newer and value <= int(current)):
                    if new == orig:
                        change.status = "already"
                        change.reason = "node already carries this value"
                    elif older:
                        change.reason = (
                            f"shares node {node.id} with a point rated {current} from a newer "
                            f"survey ({survey_day(earlier.surveyed)}); that one stays"
                        )
                    else:
                        change.reason = (
                            f"shares node {node.id} with a point rated {current}; "
                            "the higher rating stays"
                        )
                    continue
            if orig == new:
                change.status, change.reason = "already", "node already carries this value"
            elif (
                orig is not None
                and not options.force
                and not survey_newer_than_edit(change.surveyed, node.timestamp)
            ):
                change.status, change.old = "refused", orig
                change.reason = f"node has {EXPOSURE_KEY}={orig}, survey says {new}"
            else:
                change.status = "overwritten" if orig is not None else "tagged"
                change.old = orig
                if orig is not None and not options.force:
                    change.reason = (
                        f"the survey ({survey_day(change.surveyed)}) is newer than the node's "
                        f"last OSM edit ({survey_day(node.timestamp or '')})"
                    )
                node.tags[EXPOSURE_KEY] = new
                data.mark_modified(node)
                if earlier is not None and earlier.status in ("tagged", "overwritten", "already"):
                    earlier.status = "skipped"
                    earlier.reason = (
                        f"shares node {node.id} with a point rated {new} from a newer survey "
                        f"({survey_day(change.surveyed)}); that one stays"
                        if change.surveyed > earlier.surveyed
                        else f"shares node {node.id} with a point rated {new}; "
                        "the higher rating stays"
                    )
            by_node[node.id] = change
    if options.enabled and options.remove_stale:
        _remove_stale(data, result, out, options)
    return out
