"""Turn matched intervals into edits: cut ways, tag the pieces, fix relations.

Per OSM way, the intervals from every matched segment (of every track in
the batch) become claims in way chainage. Claims resolve into zones (same
tags merge, overlaps go to the longer claim), zone edges become cuts, cuts
become nodes (an existing junction node if one is close, else any existing
node if closer still, else a new node on the way's own geometry), the way
is split JOSM-style (the piece with the most nodes keeps the id), the
pieces get their tags, and every parent relation gets the pieces inserted
next to the original member in the right order.

Nothing here touches OSM. The result is an in-memory dataset for the JOSM
writer, plus a record of every decision for the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .itrs import Segment, distance_m
from .match import TrackMatch, WayPos
from .osm import Member, Node, OsmData, Relation, Way, element_type
from .tagging import existing_itrs, target_tags

ORDERED_RELATION_TYPES = frozenset({"route", "multipolygon", "boundary"})


@dataclass
class CutOptions:
    snap_m: float = 5.0
    snap_junction_m: float = 15.0
    min_piece_m: float = 15.0
    force: bool = False


@dataclass
class Claim:
    """A track's demand for tags on one stretch of a way.

    Starts as one segment's interval; same-tag claims of one track that touch
    are merged before resolution, so a track rating a whole way at one level
    counts as one long claim rather than several short ones.
    """

    track_name: str
    tags: dict[str, str]
    start_m: float
    end_m: float
    sources: list[str] = field(default_factory=list)  # "Track / S003" labels

    @property
    def length(self) -> float:
        return self.end_m - self.start_m

    @property
    def label(self) -> str:
        return ", ".join(self.sources)


@dataclass
class Zone:
    start_m: float
    end_m: float
    tags: dict[str, str]
    sources: list[str] = field(default_factory=list)  # "Track / S003" labels

    @property
    def length(self) -> float:
        return self.end_m - self.start_m


@dataclass
class Cut:
    requested_m: float
    at_m: float
    kind: str  # "junction" | "node" | "inserted"
    node_id: int
    index: int  # vertex index in the way at placement time (ways can revisit a node)

    @property
    def moved_m(self) -> float:
        return abs(self.at_m - self.requested_m)


@dataclass
class Piece:
    way_id: int
    keeps_id: bool
    node_count: int
    start_m: float
    end_m: float
    tags_before: dict[str, str]
    tags_after: dict[str, str]
    sources: list[str]

    @property
    def length(self) -> float:
        return self.end_m - self.start_m

    @property
    def changed(self) -> dict[str, tuple[str | None, str]]:
        """key -> (old, new) for every tag that changed."""
        return {
            k: (self.tags_before.get(k), v)
            for k, v in self.tags_after.items()
            if self.tags_before.get(k) != v
        }


@dataclass
class RelationChange:
    relation_id: int
    label: str
    direction: str  # "forward" | "backward" | "unknown"
    warning: str | None = None
    role: str = ""  # copied from the original member onto every piece, as JOSM does


@dataclass
class Refusal:
    way_id: int
    key: str
    old: str
    new: str
    sources: list[str]


@dataclass
class WayChange:
    way_id: int
    name: str
    highway: str
    length_m: float
    zones: list[Zone] = field(default_factory=list)
    cuts: list[Cut] = field(default_factory=list)
    pieces: list[Piece] = field(default_factory=list)
    relations: list[RelationChange] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    modified: bool = False

    @property
    def status(self) -> str:
        if self.refusals:
            return "refused"
        if not self.modified:
            return "unchanged"
        return "split" if len(self.pieces) > 1 else "tagged"


@dataclass
class ApplyResult:
    ways: list[WayChange]
    nodes_added: int = 0
    ways_added: int = 0
    relations_modified: int = 0

    @property
    def refusals(self) -> list[Refusal]:
        return [r for w in self.ways for r in w.refusals]

    @property
    def modified_ways(self) -> list[WayChange]:
        return [w for w in self.ways if w.modified]


def _chainage(data: OsmData, way: Way) -> list[float]:
    coords = data.way_coords(way)
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + distance_m(a, b))
    return cum


def _pos_to_m(pos: WayPos, cum: list[float]) -> float:
    index, t = pos
    if index >= len(cum) - 1:
        return cum[-1]
    index = max(0, index)
    return cum[index] + t * (cum[index + 1] - cum[index])


def _label(track_name: str, segment: Segment) -> str:
    return f"{track_name} / {segment.name}"


def _collect_claims(data: OsmData, matches: list[TrackMatch]) -> dict[int, list[Claim]]:
    claims: dict[int, list[Claim]] = {}
    cums: dict[int, list[float]] = {}
    for tm in matches:
        for sm in tm.segments:
            tags = target_tags(sm.segment)
            if not tags:
                continue  # paved road or nothing rated: nothing to write, no cuts
            for iv in sm.intervals:
                way = data.ways.get(iv.way_id)
                if way is None:
                    continue
                cum = cums.get(way.id)
                if cum is None:
                    cum = cums[way.id] = _chainage(data, way)
                start = _pos_to_m(iv.start_pos, cum)
                end = _pos_to_m(iv.end_pos, cum)
                if end < start:
                    start, end = end, start
                claims.setdefault(way.id, []).append(
                    Claim(
                        tm.track.name, dict(tags), start, end, [_label(tm.track.name, sm.segment)]
                    )
                )
    return {way_id: _merge_same_track(c) for way_id, c in claims.items()}


def _merge_same_track(claims: list[Claim]) -> list[Claim]:
    """Merge touching same-tag claims of the same track."""
    out: list[Claim] = []
    by_key: dict[tuple[str, tuple[tuple[str, str], ...]], list[Claim]] = {}
    for c in claims:
        by_key.setdefault((c.track_name, tuple(sorted(c.tags.items()))), []).append(c)
    for group in by_key.values():
        group.sort(key=lambda c: c.start_m)
        current = group[0]
        for c in group[1:]:
            if c.start_m <= current.end_m + 0.5:
                current.end_m = max(current.end_m, c.end_m)
                current.sources.extend(s for s in c.sources if s not in current.sources)
            else:
                out.append(current)
                current = c
        out.append(current)
    return out


def _resolve_zones(claims: list[Claim], notes: list[str], min_piece_m: float) -> list[Zone]:
    """Longer claims win overlaps; equal tags merge; result sorted by chainage.

    A leftover of a trimmed claim shorter than the minimum piece is dropped
    (with a note) rather than becoming a sliver. A short claim that was not
    trimmed at all is kept: a short segment is real data.
    """
    zones: list[Zone] = []
    for claim in sorted(claims, key=lambda c: -c.length):
        parts = [(claim.start_m, claim.end_m)]
        for z in zones:
            if z.tags == claim.tags:
                continue
            trimmed: list[tuple[float, float]] = []
            for a, b in parts:
                if b <= z.start_m or a >= z.end_m:
                    trimmed.append((a, b))
                    continue
                if a < z.start_m:
                    trimmed.append((a, z.start_m))
                if b > z.end_m:
                    trimmed.append((z.end_m, b))
                notes.append(
                    f"{claim.label} overlaps {', '.join(z.sources)} between "
                    f"{max(a, z.start_m):.0f} and {min(b, z.end_m):.0f} m with different "
                    "ratings; the longer claim wins"
                )
            parts = trimmed
        for a, b in parts:
            trimmed_part = (b - a) < claim.length - 0.5
            if b - a < 1.0 or (trimmed_part and b - a < min_piece_m):
                if b - a >= 1.0:
                    notes.append(
                        f"{b - a:.0f} m of {claim.label} between {a:.0f} and {b:.0f} m dropped "
                        f"(leftover after overlap, shorter than {min_piece_m:.0f} m)"
                    )
                continue
            zones.append(Zone(a, b, dict(claim.tags), list(claim.sources)))
    zones.sort(key=lambda z: z.start_m)
    merged: list[Zone] = []
    for z in zones:
        last = merged[-1] if merged else None
        if last is not None and last.tags == z.tags and z.start_m <= last.end_m + 0.5:
            last.end_m = max(last.end_m, z.end_m)
            last.sources.extend(s for s in z.sources if s not in last.sources)
        else:
            merged.append(z)
    return merged


def _tidy_zones(zones: list[Zone], length_m: float, min_piece_m: float, notes: list[str]) -> None:
    """Avoid slivers at the way ends and between zones (in place)."""
    if not zones:
        return
    first, last = zones[0], zones[-1]
    if 0.5 < first.start_m < min_piece_m:
        notes.append(
            f"rating extended {first.start_m:.0f} m to the way start (shorter than "
            f"{min_piece_m:.0f} m)"
        )
        first.start_m = 0.0
    if 0.5 < length_m - last.end_m < min_piece_m:
        notes.append(
            f"rating extended {length_m - last.end_m:.0f} m to the way end (shorter than "
            f"{min_piece_m:.0f} m)"
        )
        last.end_m = length_m
    for a, b in zip(zones, zones[1:]):
        gap = b.start_m - a.end_m
        if 0.5 < gap < min_piece_m:
            notes.append(
                f"{gap:.0f} m gap between {', '.join(a.sources)} and {', '.join(b.sources)} "
                f"closed (shorter than {min_piece_m:.0f} m)"
            )
            a.end_m = b.start_m
    # Zones may now touch; merge equal neighbors again.
    i = 0
    while i < len(zones) - 1:
        a, b = zones[i], zones[i + 1]
        if a.tags == b.tags and b.start_m <= a.end_m + 0.5:
            a.end_m = max(a.end_m, b.end_m)
            a.sources.extend(s for s in b.sources if s not in a.sources)
            del zones[i + 1]
        else:
            i += 1


def _is_junction(data: OsmData, node_id: int) -> bool:
    """Shared with another highway way (an area outline or fence doesn't count)."""
    return sum(1 for w in data.parent_ways(node_id) if w.tags.get("highway")) > 1


def _place_cut(
    data: OsmData,
    way: Way,
    requested_m: float,
    options: CutOptions,
    used: set[int],
    max_move_m: float,
) -> Cut:
    """Choose or create the node for a cut, inserting into the way if needed.

    Never the way's end nodes (that would make a one-node piece), never a
    node another cut already uses, and never further than ``max_move_m`` (half
    the distance to the neighboring cut or way end), so two cuts can't
    collapse into one and silently erase the zone between them.
    """
    cum = _chainage(data, way)
    best_junction: tuple[float, int] | None = None
    best_node: tuple[float, int] | None = None
    for k in range(1, len(way.nodes) - 1):
        if way.nodes[k] in used:
            continue
        d = abs(cum[k] - requested_m)
        if d > max_move_m:
            continue
        if d <= options.snap_junction_m and _is_junction(data, way.nodes[k]):
            if best_junction is None or d < best_junction[0]:
                best_junction = (d, k)
        if d <= options.snap_m and (best_node is None or d < best_node[0]):
            best_node = (d, k)
    if best_junction is not None:
        k = best_junction[1]
        return Cut(requested_m, cum[k], "junction", way.nodes[k], k)
    if best_node is not None:
        k = best_node[1]
        return Cut(requested_m, cum[k], "node", way.nodes[k], k)

    node, index = insert_node_at(data, way, cum, requested_m)
    return Cut(requested_m, requested_m, "inserted", node.id, index)


def insert_node_at(data: OsmData, way: Way, cum: list[float], along_m: float) -> tuple[Node, int]:
    """Insert a new node on the way's own geometry at chainage ``along_m``.

    Returns the node and its index in ``way.nodes``. The caller marks the
    way modified when it isn't going to be split or retagged anyway.
    """
    k = max(i for i in range(len(cum) - 1) if cum[i] <= along_m)
    span = cum[k + 1] - cum[k]
    t = 0.0 if span == 0.0 else min(1.0, (along_m - cum[k]) / span)
    a = data.nodes[way.nodes[k]]
    b = data.nodes[way.nodes[k + 1]]
    node = Node(
        id=data.new_id(),
        lat=round(a.lat + t * (b.lat - a.lat), 7),
        lon=round(a.lon + t * (b.lon - a.lon), 7),
    )
    data.add(node)
    nodes = list(way.nodes)
    nodes.insert(k + 1, node.id)
    data.set_way_nodes(way, nodes)
    return node, k + 1


def _way_direction(data: OsmData, relation: Relation, index: int, first: int, last: int) -> str:
    """forward | backward | unknown, from the nearest way members on either side.

    ``first``/``last`` are the ORIGINAL way's end nodes, captured before the
    split truncated the keeper piece.
    """
    if first == last:
        return "forward"  # closed way: order is irrelevant
    members = relation.members

    def ends(i: int) -> tuple[int, int] | None:
        m = members[i]
        if m.type != "way":
            return None
        w = data.ways.get(m.ref)
        if w is None or not w.nodes:
            return None
        return (w.nodes[0], w.nodes[-1])

    neighbors = 0
    for i in range(index - 1, -1, -1):
        if members[i].type != "way":
            continue
        neighbors += 1
        e = ends(i)
        if e is None:
            break
        if first in e:
            return "forward"
        if last in e:
            return "backward"
        break
    for i in range(index + 1, len(members)):
        if members[i].type != "way":
            continue
        neighbors += 1
        e = ends(i)
        if e is None:
            break
        if last in e:
            return "forward"
        if first in e:
            return "backward"
        break
    # The only way member: order can't matter (JOSM calls this "irrelevant").
    return "forward" if neighbors == 0 else "unknown"


def _fix_relations(
    data: OsmData,
    way: Way,
    pieces_in_order: list[Way],
    change: WayChange,
    original_ends: tuple[int, int],
) -> int:
    """Insert split pieces next to the original member in every parent relation."""
    count = 0
    first, last = original_ends
    for relation in data.parent_relations(way):
        new_members: list[Member] = []
        touched = False
        for index, m in enumerate(relation.members):
            if m.type != "way" or m.ref != way.id:
                new_members.append(m)
                continue
            direction = _way_direction(data, relation, index, first, last)
            ordered = (
                pieces_in_order if direction != "backward" else list(reversed(pieces_in_order))
            )
            new_members.extend(Member("way", p.id, m.role) for p in ordered)
            touched = True
            rtype = relation.tags.get("type", "")
            label = relation.tags.get("name") or f"{rtype} {relation.tags.get('route', '')}".strip()
            warning = None
            if direction == "unknown":
                warning = "direction unknown (neighboring members not downloaded); check order"
            elif rtype not in ORDERED_RELATION_TYPES:
                warning = f"relation type {rtype!r} is not an ordered type; check membership"
            change.relations.append(
                RelationChange(relation.id, label, direction, warning, role=m.role)
            )
        if touched:
            relation.members = new_members
            data.invalidate_index()
            data.mark_modified(relation)
            count += 1
    return count


def _zone_for(zones: list[Zone], start_m: float, end_m: float) -> Zone | None:
    """The zone covering most of a piece, if it covers at least half of it."""
    best, best_overlap = None, 0.0
    for z in zones:
        overlap = min(end_m, z.end_m) - max(start_m, z.start_m)
        if overlap > best_overlap:
            best, best_overlap = z, overlap
    if best is not None and best_overlap >= 0.5 * max(end_m - start_m, 1e-9):
        return best
    return None


def _plan_ranges(
    data: OsmData, way: Way, cuts: list[Cut], zones: list[Zone]
) -> list[tuple[int, int, Zone | None]]:
    """(first index, last index, zone) per piece the current cuts would make."""
    cum = _chainage(data, way)
    bounds = [0, *(c.index for c in cuts), len(way.nodes) - 1]
    return [
        (bounds[i], bounds[i + 1], _zone_for(zones, cum[bounds[i]], cum[bounds[i + 1]]))
        for i in range(len(bounds) - 1)
    ]


def apply_matches(
    data: OsmData, matches: list[TrackMatch], options: CutOptions | None = None
) -> ApplyResult:
    """Apply every track's matched intervals to the dataset, in place."""
    options = options or CutOptions()
    result = ApplyResult(ways=[])
    all_claims = _collect_claims(data, matches)

    for way_id in sorted(all_claims):
        way = data.ways[way_id]
        cum = _chainage(data, way)
        length_m = cum[-1]
        change = WayChange(
            way_id=way.id,
            name=way.tags.get("name", ""),
            highway=way.tags.get("highway", ""),
            length_m=length_m,
        )
        result.ways.append(change)

        zones = _resolve_zones(all_claims[way_id], change.notes, options.min_piece_m)
        _tidy_zones(zones, length_m, options.min_piece_m, change.notes)
        change.zones = zones
        if not zones:
            continue

        # Refuse when an existing ITRS value would change, unless forced.
        existing = existing_itrs(way.tags)
        for z in zones:
            for k, v in z.tags.items():
                if k in existing and existing[k] != v:
                    change.refusals.append(Refusal(way.id, k, existing[k], v, list(z.sources)))
        if change.refusals and not options.force:
            continue
        change.refusals = []

        # Cuts: every zone edge strictly inside the way. Each may move at most
        # halfway to its neighbors, so cuts never merge or cross.
        wanted = sorted(
            {m for z in zones for m in (z.start_m, z.end_m) if 0.5 < m < length_m - 0.5}
        )
        original_ends = (way.nodes[0], way.nodes[-1])
        used: set[int] = set()
        for n, requested in enumerate(wanted):
            before_m = wanted[n - 1] if n > 0 else 0.0
            after_m = wanted[n + 1] if n < len(wanted) - 1 else length_m
            max_move = min(
                options.snap_junction_m, (requested - before_m) / 2, (after_m - requested) / 2
            )
            cut = _place_cut(data, way, requested, options, used, max_move)
            used.add(cut.node_id)
            change.cuts.append(cut)
        change.cuts.sort(key=lambda c: c.index)

        # A cut between two pieces that would end up with the same tags is
        # pointless (a snapped cut can do that); remove it, and any node it
        # inserted, before splitting.
        ranges_planned = _plan_ranges(data, way, change.cuts, zones)
        for i in range(len(ranges_planned) - 1, 0, -1):
            za, zb = ranges_planned[i - 1][2], ranges_planned[i][2]
            if (za.tags if za else None) == (zb.tags if zb else None):
                cut = change.cuts.pop(i - 1)
                if cut.kind == "inserted":
                    nodes = list(way.nodes)
                    nodes.pop(cut.index)
                    data.set_way_nodes(way, nodes)
                    del data.nodes[cut.node_id]
                    for later in change.cuts[i - 1 :]:
                        later.index -= 1
                change.notes.append(
                    f"cut at {cut.requested_m:.0f} m dropped: both sides would carry the same tags"
                )
                ranges_planned = _plan_ranges(data, way, change.cuts, zones)
        result.nodes_added += sum(1 for c in change.cuts if c.kind == "inserted")
        cum = _chainage(data, way)

        # Split at the cut nodes, JOSM-style: most nodes keeps the id.
        ranges = [(a, b) for a, b, _ in ranges_planned]
        keeper = max(range(len(ranges)), key=lambda i: (ranges[i][1] - ranges[i][0], -i))
        original_tags = dict(way.tags)
        all_nodes = list(way.nodes)  # slice from the untouched list; the keeper shrinks below
        pieces_in_order: list[Way] = []
        pieces: list[Piece] = []
        for i, (a, b) in enumerate(ranges):
            nodes = all_nodes[a : b + 1]
            if i == keeper:
                piece_way = way
                if len(ranges) > 1:
                    data.set_way_nodes(way, nodes)
            else:
                piece_way = Way(id=data.new_id(), nodes=list(nodes), tags=dict(original_tags))
                data.add(piece_way)
                result.ways_added += 1
            pieces_in_order.append(piece_way)
            start_m, end_m = cum[a], cum[b]
            best = ranges_planned[i][2]
            after = dict(piece_way.tags)
            sources: list[str] = []
            if best is not None:
                after.update(best.tags)
                sources = list(best.sources)
            pieces.append(
                Piece(
                    way_id=piece_way.id,
                    keeps_id=i == keeper,
                    node_count=len(nodes),
                    start_m=start_m,
                    end_m=end_m,
                    tags_before=dict(piece_way.tags),
                    tags_after=after,
                    sources=sources,
                )
            )
            piece_way.tags = after

        covered = {id(z) for _, _, z in ranges_planned if z is not None}
        for z in zones:
            if id(z) not in covered:
                change.notes.append(
                    f"rating from {', '.join(z.sources)} ({z.start_m:.0f} to {z.end_m:.0f} m) "
                    "did not land on any piece after cuts were placed; check in JOSM"
                )

        change.pieces = pieces
        split = len(ranges) > 1
        tag_change = any(p.changed for p in pieces)
        if split or tag_change:
            data.mark_modified(way)
            change.modified = True
        else:
            change.notes.append("already tagged with these values; nothing to change")
            # Undo bookkeeping for new elements: none were created without a split.
        if split:
            result.relations_modified += _fix_relations(
                data, way, pieces_in_order, change, original_ends
            )
        else:
            # No split: leave relations alone. The single piece is the way itself.
            pass

    return result


def modified_summary(data: OsmData) -> dict[str, int]:
    counts = {"node": 0, "way": 0, "relation": 0}
    for element in data.modified():
        counts[element_type(element)] += 1
    return counts
