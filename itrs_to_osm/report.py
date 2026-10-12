"""Everything one run produced, and the Markdown report written from it.

``RunContext`` is the single structure the report renderers consume. The
CLI fills it. ``build_report`` turns it into a small list of blocks
(headings, paragraphs, lists, tables, code), and ``write_markdown`` renders
those blocks as Markdown. ``html_report.write_html`` renders the same blocks
as HTML and adds a map, so both reports always say the same thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .itrs import DIRECTION_ONE_WAY, DIRECTION_TWO_WAY, USE_BIKE_ONLY, USE_SHARED, Segment, Track
from .match import Interval, MatchOptions, SegmentMatch, TrackMatch
from .osm import OsmData
from .split import ApplyResult, CutOptions, WayChange
from .tagging import (
    EXPOSURE_KEY,
    ITRS_KEYS,
    TECH_KEY,
    existing_itrs,
    level_name,
    skipped_keys,
)


@dataclass
class RunContext:
    tracks: list[Track]
    matches: list[TrackMatch]
    result: ApplyResult
    data: OsmData
    match_options: MatchOptions
    cut_options: CutOptions
    osm_source: str  # "Overpass API" or the local file used
    osm_base: str | None  # replication timestamp of the download, when known
    generated_at: str  # ISO timestamp of this run
    tool_version: str
    osm_path: Path | None  # the .osm written, or None for --dry-run
    warnings: list[str] = field(default_factory=list)


def changeset_suggestion(ctx: RunContext) -> tuple[str, str]:
    """(comment, source) to paste into JOSM's upload dialog."""
    names = ", ".join(t.name for t in ctx.tracks)
    versions = sorted({v for t in ctx.tracks for v in t.itrs_versions})
    dates = sorted({d for t in ctx.tracks for d in t.survey_dates})
    comment = f"Add ITRS trail ratings (technical difficulty, exposure) for {names}"
    source = "ITRS trail rating survey"
    if versions:
        source += f" (ITRS {', '.join(versions)})"
    if dates:
        source += f", surveyed {dates[0]}" + (f" to {dates[-1]}" if len(dates) > 1 else "")
    source += f"; itrs-to-osm {ctx.tool_version}"
    return comment, source


# --- Report document model ---------------------------------------------------
#
# Both renderers walk the same list of blocks. Inline content is a plain
# string or a list of strings and the three marked-up parts below; renderers
# do all escaping, so builders never write Markdown or HTML syntax.


@dataclass(frozen=True)
class Strong:
    text: str


@dataclass(frozen=True)
class Code:
    text: str


@dataclass(frozen=True)
class Link:
    text: str
    url: str


InlinePart = str | Strong | Code | Link
Inline = InlinePart | list[Any]  # a part, or a (nested) list of parts


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Para:
    content: Inline


@dataclass
class ItemList:
    items: list[Inline]
    ordered: bool = False


@dataclass
class Table:
    header: list[str]
    rows: list[list[Inline]]


@dataclass
class CodeBlock:
    text: str


Block = Heading | Para | ItemList | Table | CodeBlock


def inline_parts(content: Inline) -> list[InlinePart]:
    """Flatten inline content to a list of parts."""
    if isinstance(content, list):
        out: list[InlinePart] = []
        for c in content:
            out.extend(inline_parts(c))
        return out
    return [content]


def _join(items: list[Inline], sep: str = ", ") -> list[Any]:
    out: list[Any] = []
    for i, item in enumerate(items):
        if i:
            out.append(sep)
        out.append(item)
    return out


# --- Small formatting helpers -------------------------------------------------

OSM_WAY_URL = "https://www.openstreetmap.org/way/{}"
FOOT_ALLOWED = frozenset({"yes", "designated", "permissive"})
# highway values where OSM implies foot access when no foot tag says otherwise
FOOT_IMPLIED_HIGHWAYS = frozenset({"path", "track", "footway", "pedestrian", "steps"})
ONEWAY_VALUES = frozenset({"yes", "1", "true", "-1"})


def way_ref(way_id: int) -> str:
    """JOSM-style way reference: w123."""
    return f"w{way_id}"


def piece_ref(way_id: int, keeps_id: bool) -> str:
    if way_id < 0:
        return f"new way {way_id}"
    return f"w{way_id} (keeps id)" if keeps_id else f"w{way_id}"


def rating_text(key: str, value: int) -> str:
    """'3 (Advanced)', 'not rated', or '7 (out of range)'."""
    if value == 0:
        return "not rated"
    name = level_name(key, value)
    return f"{value} ({'out of range' if name == '?' else name})"


def segment_label(track: Track, segment: Segment) -> str:
    return f"{track.name} / {segment.name}"


def segment_length(segment: Segment) -> float:
    return segment.length_m if segment.length_m is not None else segment.geometry_length_m()


def nothing_to_write(segment: Segment) -> bool:
    """True for a paved road or a segment with no writable rating at all."""
    return {k for k, _ in skipped_keys(segment)} >= set(ITRS_KEYS)


def segment_status(sm: SegmentMatch) -> str:
    """matched | partial | unmatched | not applied (matched, nothing to write)."""
    if sm.status != "unmatched" and nothing_to_write(sm.segment):
        return "not applied"
    return sm.status


def _key_order(key: str) -> tuple[int, str]:
    return (ITRS_KEYS.index(key) if key in ITRS_KEYS else len(ITRS_KEYS), key)


def _itrs_tags_cell(tags: dict[str, str], bold: set[str] | None = None) -> Inline:
    itrs = existing_itrs(tags)
    if not itrs:
        return "none"
    parts: list[Any] = []
    for key in sorted(itrs, key=_key_order):
        if parts:
            parts.append("; ")
        parts.append(f"{key}=")
        parts.append(Strong(itrs[key]) if bold and key in bold else itrs[key])
    return parts


def _way_title(ctx: RunContext, way_id: int) -> str:
    way = ctx.data.ways.get(way_id)
    name = way.tags.get("name", "") if way else ""
    return f"{way_ref(way_id)} {name}" if name else way_ref(way_id)


def _way_status_words(change: WayChange) -> str:
    if change.status == "split":
        return f"split into {len(change.pieces)} pieces"
    return change.status


def _segments_by_way(sm: SegmentMatch) -> dict[int, list[Interval]]:
    out: dict[int, list[Interval]] = {}
    for iv in sm.intervals:
        out.setdefault(iv.way_id, []).append(iv)
    return out


# --- Sections -----------------------------------------------------------------


def _header(ctx: RunContext) -> list[Block]:
    mo, co = ctx.match_options, ctx.cut_options
    source = ctx.osm_source + (f", as of {ctx.osm_base}" if ctx.osm_base else "")
    output: Inline = Code(str(ctx.osm_path)) if ctx.osm_path else "dry run, no .osm written"
    thresholds = (
        f"corridor {mo.corridor_m:g} m, max offset {mo.max_offset_m:g} m (90th percentile), "
        f"snap {co.snap_m:g} m, snap to junction {co.snap_junction_m:g} m, "
        f"min piece {co.min_piece_m:g} m, force {'on' if co.force else 'off'}"
    )
    items: list[Inline] = [
        ["Generated: ", ctx.generated_at],
        ["Tool: itrs-to-osm ", ctx.tool_version],
        ["OSM data: ", source],
        ["Output: ", output],
        ["Thresholds: ", thresholds],
    ]
    if mo.name_hint:
        items.append(["Matching pinned to the name: ", mo.name_hint])
    if mo.way_ids:
        items.append(
            ["Matching restricted to: ", ", ".join(way_ref(i) for i in sorted(mo.way_ids))]
        )
    blocks: list[Block] = [Heading(1, "itrs-to-osm report"), ItemList(items)]
    warnings: list[str] = []
    for w in [*ctx.warnings, *(f"{t.name}: {w}" for t in ctx.tracks for w in t.warnings)]:
        if w not in warnings:
            warnings.append(w)
    if warnings:
        blocks.append(Para(Strong("Warnings:")))
        blocks.append(ItemList(list(warnings)))
    return blocks


def _summary(ctx: RunContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, "Summary")]
    lines: list[Inline] = []
    for t in ctx.tracks:
        versions = ", ".join(t.itrs_versions) or "unknown"
        dates = ", ".join(t.survey_dates) or "unknown"
        length = t.length_m if t.length_m is not None else sum(map(segment_length, t.segments))
        lines.append(
            [
                Strong(t.name),
                f": ITRS {versions}, surveyed {dates}, exported {t.export_date or 'unknown'}, "
                f"{len(t.segments)} segments, {length:.0f} m",
            ]
        )
    blocks.append(ItemList(lines))

    statuses = [segment_status(sm) for tm in ctx.matches for sm in tm.segments]
    r = ctx.result
    split = sum(w.status == "split" for w in r.ways)
    totals: list[Inline] = [
        f"Segments: {statuses.count('matched')} matched, {statuses.count('partial')} partial, "
        f"{statuses.count('unmatched')} unmatched, {statuses.count('not applied')} not applied "
        "(paved road or nothing rated)",
        f"Ways modified: {len(r.modified_ways)} ({split} split)",
        f"New ways from splits: {r.ways_added}",
        f"Nodes added: {r.nodes_added}",
        f"Relations updated: {r.relations_modified}",
        f"Refusals: {len(r.refusals)}",
    ]
    blocks.append(Para(Strong("Totals:")))
    blocks.append(ItemList(totals))
    if r.refusals:
        ways = sorted({x.way_id for x in r.refusals})
        blocks.append(
            Para(
                Strong(
                    f"{len(r.refusals)} existing ITRS value(s) differ from the survey on "
                    f"{', '.join(way_ref(w) for w in ways)}. Nothing was written for those ways "
                    "(no split, no tags). Rerun with --force to overwrite them."
                )
            )
        )
    return blocks


def _changeset(ctx: RunContext) -> list[Block]:
    comment, source = changeset_suggestion(ctx)
    return [
        Heading(2, "Suggested changeset tags"),
        Para(
            "Paste these into the changeset tags in JOSM's upload dialog. Provenance belongs "
            "in the changeset, not on the objects, so no source tag is added to anything edited."
        ),
        CodeBlock(f"comment={comment}\nsource={source}"),
    ]


def _name_match(tm: TrackMatch) -> Inline:
    if not tm.name_matches:
        return [Strong("Name match:"), " no name match; matched by geometry alone."]
    groups: dict[tuple[str, float], list[int]] = {}
    for way_id, (similarity, where) in tm.name_matches.items():
        prefix = f"way {way_id} name "
        label = "way name " + where[len(prefix) :] if where.startswith(prefix) else where
        groups.setdefault((label, round(similarity, 2)), []).append(way_id)
    parts: list[Inline] = []
    for (label, similarity), ids in sorted(groups.items(), key=lambda kv: (-kv[0][1], kv[0][0])):
        parts.append(
            f"{', '.join(way_ref(i) for i in sorted(ids))} by {label} (similarity {similarity:.2f})"
        )
    return [Strong("Name match:"), " ", *_join(parts, "; ")]


def _track_section(ctx: RunContext, tm: TrackMatch) -> list[Block]:
    t = tm.track
    blocks: list[Block] = [Heading(2, f"Track: {t.name}")]
    rows: list[list[Inline]] = []
    for sm in tm.segments:
        s = sm.segment
        offset = f"{sm.p90_offset:.1f} m" if sm.p90_offset > 0 else "-"
        status = segment_status(sm)
        if status == "not applied":
            status = f"{sm.status}, not applied"
        rows.append(
            [
                s.name,
                rating_text(TECH_KEY, s.tech_rating),
                rating_text(EXPOSURE_KEY, s.max_exposure),
                s.track_type or "?",
                s.use or "?",
                s.direction or "?",
                f"{segment_length(s):.0f} m",
                status,
                ", ".join(way_ref(w) for w in sm.way_ids) or "none",
                offset,
            ]
        )
    blocks.append(
        Table(
            [
                "Segment",
                "Tech",
                "Exposure",
                "Type",
                "Use",
                "Direction",
                "Length",
                "Status",
                "OSM way(s)",
                "Offset p90",
            ],
            rows,
        )
    )
    notes: list[Inline] = []
    for sm in tm.segments:
        status = segment_status(sm)
        if status == "matched":
            continue
        reasons = list(sm.reasons)
        if status == "not applied":
            reasons += [r for r in dict.fromkeys(reason for _, reason in skipped_keys(sm.segment))]
        notes.append([Strong(sm.segment.name), f" ({status}): ", "; ".join(reasons) or "no reason"])
    if notes:
        blocks.append(ItemList(notes))
    blocks.append(Para(_name_match(tm)))
    return blocks


def _cut_text(cut: Any) -> str:
    where = f"cut at {cut.requested_m:.0f} m: "
    if cut.kind == "junction":
        return where + f"snapped to junction node {cut.node_id} (moved {cut.moved_m:.1f} m)"
    if cut.kind == "node":
        return where + f"snapped to existing node {cut.node_id} (moved {cut.moved_m:.1f} m)"
    return where + f"new node inserted (id {cut.node_id})"


def _ways_section(ctx: RunContext) -> list[Block]:
    blocks: list[Block] = [Heading(2, "Ways")]
    if not ctx.result.ways:
        blocks.append(Para("No way received a rating."))
        return blocks
    for w in ctx.result.ways:
        title = (
            f"{way_ref(w.way_id)} {w.name or '(unnamed)'} "
            f"({w.highway or 'no highway tag'}, {w.length_m:.0f} m): {_way_status_words(w)}"
        )
        blocks.append(Heading(3, title))
        url = OSM_WAY_URL.format(w.way_id)
        blocks.append(Para(Link(url, url)))
        if w.cuts:
            cuts = sorted(w.cuts, key=lambda c: c.requested_m)
            blocks.append(ItemList([_cut_text(c) for c in cuts]))
        if w.pieces:
            rows: list[list[Inline]] = []
            for p in sorted(w.pieces, key=lambda p: p.start_m):
                rows.append(
                    [
                        piece_ref(p.way_id, p.keeps_id and len(w.pieces) > 1),
                        f"{p.start_m:.0f} m",
                        f"{p.end_m:.0f} m",
                        str(p.node_count),
                        _itrs_tags_cell(p.tags_before),
                        _itrs_tags_cell(p.tags_after, set(p.changed)),
                        ", ".join(p.sources) or "none (outside the track)",
                    ]
                )
            blocks.append(
                Table(
                    [
                        "Piece",
                        "From",
                        "To",
                        "Nodes",
                        "Tags before",
                        "Tags after",
                        "From segments",
                    ],
                    rows,
                )
            )
        items: list[Inline] = []
        for rc in w.relations:
            rel = ctx.data.relations.get(rc.relation_id)
            rtype = rel.tags.get("type", "?") if rel else "?"
            how = (
                "pieces inserted, direction unknown"
                if rc.direction == "unknown"
                else f"pieces inserted {rc.direction}"
            )
            line = f"relation {rc.relation_id} {rc.label} ({rtype}): {how}"
            if rc.role:
                line += (
                    f"; the pieces carry role '{rc.role}', copied from the original member "
                    "(a validator warning about this role is about the existing tagging)"
                )
            if rc.warning:
                line += f"; warning: {rc.warning}"
            items.append(line)
        for r in w.refusals:
            items.append(
                [
                    Strong("Refused: "),
                    f"{r.key} is {r.old} on this way, the survey says {r.new} "
                    f"(from {', '.join(r.sources)}); not changed",
                ]
            )
        items += [["Note: ", n] for n in w.notes]
        if items:
            blocks.append(ItemList(items))
    return blocks


def _not_applied(ctx: RunContext) -> list[Block]:
    mo = ctx.match_options
    lines: list[Inline] = []
    for tm in ctx.matches:
        for sm in tm.segments:
            seg = sm.segment
            label = segment_label(tm.track, seg)
            if sm.status == "unmatched":
                lines.append(f"{label}: unmatched ({'; '.join(sm.reasons) or 'no reason'})")
                continue
            for iv in sm.dropped:
                if iv.length < mo.min_piece_m:
                    why = f"shorter than the {mo.min_piece_m:g} m minimum piece"
                else:
                    why = (
                        f"90th percentile offset {iv.p90_offset:.1f} m exceeds "
                        f"{mo.max_offset_m:g} m"
                    )
                lines.append(f"{label}: {iv.length:.0f} m on {way_ref(iv.way_id)} dropped ({why})")
            if sm.unmatched_vertices:
                lines.append(
                    f"{label}: {sm.unmatched_vertices} of {sm.total_vertices} vertices "
                    "off-network (no way within the corridor there)"
                )
            skipped = skipped_keys(seg)
            if seg.is_paved_road:
                ways = ", ".join(way_ref(w) for w in sm.way_ids)
                lines.append(f"{label} on {ways}: {skipped[0][1]}")
                continue
            for key, reason in skipped:
                lines.append([f"{label}: ", Code(key), f" {reason}"])
    for w in ctx.result.ways:
        for r in w.refusals:
            lines.append(
                f"{', '.join(r.sources)}: nothing written to {way_ref(w.way_id)}, existing "
                f"{r.key}={r.old} differs from the survey's {r.new} (refused)"
            )
        for n in w.notes:
            if "longer claim wins" in n:
                lines.append(f"{way_ref(w.way_id)}: {n}")
    return [Heading(2, "Not applied"), ItemList(lines) if lines else Para("Nothing.")]


def _overwritten(ctx: RunContext) -> list[Inline]:
    out: list[Inline] = []
    for w in ctx.result.ways:
        for p in w.pieces:
            for key, (old, new) in sorted(p.changed.items(), key=lambda kv: _key_order(kv[0])):
                if old is None or key not in ITRS_KEYS:
                    continue
                ref = way_ref(p.way_id) if p.way_id > 0 else f"new way {p.way_id}"
                out.append(
                    f"{ref} ({w.name or 'unnamed'}): {key} {old} -> {new} "
                    f"(from {', '.join(p.sources) or '?'})"
                )
    return out


def _direction_mismatches(ctx: RunContext) -> list[Inline]:
    found: dict[tuple[int, str], list[str]] = {}
    for tm in ctx.matches:
        for sm in tm.segments:
            seg = sm.segment
            if seg.direction not in (DIRECTION_ONE_WAY, DIRECTION_TWO_WAY):
                continue
            for way_id, ivs in _segments_by_way(sm).items():
                way = ctx.data.ways.get(way_id)
                if way is None:
                    continue
                oneway = way.tags.get("oneway")
                osm_one = oneway in ONEWAY_VALUES
                if seg.is_one_way and not osm_one:
                    has = f"oneway={oneway}" if oneway else "no oneway tag"
                    msg = f"ITRS says {DIRECTION_ONE_WAY}, OSM has {has} (two-way)"
                elif not seg.is_one_way and osm_one:
                    msg = f"ITRS says {DIRECTION_TWO_WAY}, OSM has oneway={oneway}"
                elif seg.is_one_way:
                    against = sum(iv.length for iv in ivs if iv.reversed) > sum(
                        iv.length for iv in ivs if not iv.reversed
                    )
                    if against == (oneway == "-1"):
                        continue
                    msg = (
                        f"ITRS says {DIRECTION_ONE_WAY} and OSM has oneway={oneway}, but the "
                        "survey ran the opposite way"
                    )
                else:
                    continue
                found.setdefault((way_id, msg), []).append(seg.name)
    return [
        f"{_way_title(ctx, way_id)}: {msg} ({', '.join(segs)})"
        for (way_id, msg), segs in found.items()
    ]


def _use_mismatches(ctx: RunContext) -> list[Inline]:
    found: dict[tuple[int, str], list[str]] = {}
    for tm in ctx.matches:
        for sm in tm.segments:
            seg = sm.segment
            for way_id in sm.way_ids:
                way = ctx.data.ways.get(way_id)
                if way is None:
                    continue
                foot = way.tags.get("foot")
                highway = way.tags.get("highway", "")
                msgs: list[str] = []
                if seg.use == USE_BIKE_ONLY:
                    if foot in FOOT_ALLOWED:
                        msgs.append(f"ITRS says {USE_BIKE_ONLY}, OSM has foot={foot}")
                    elif foot is None and highway in FOOT_IMPLIED_HIGHWAYS:
                        msgs.append(
                            f"ITRS says {USE_BIKE_ONLY}, OSM has no foot tag, and "
                            f"highway={highway} allows walking by default"
                        )
                    elif foot is None:
                        msgs.append(
                            f"ITRS says {USE_BIKE_ONLY}, OSM has no foot tag (default foot "
                            f"access on highway={highway or '?'} varies by country)"
                        )
                elif seg.use == USE_SHARED and foot == "no":
                    msgs.append(f"ITRS says {USE_SHARED}, OSM has foot=no")
                if way.tags.get("bicycle") == "no":
                    msgs.append("OSM has bicycle=no on a rated trail")
                for msg in msgs:
                    found.setdefault((way_id, msg), []).append(seg.name)
    return [
        f"{_way_title(ctx, way_id)}: {msg} ({', '.join(segs)})"
        for (way_id, msg), segs in found.items()
    ]


def _point_details(point: Any) -> str:
    if not point.point_type:
        return ""
    prefix = f"point_{point.point_type[0].lower()}{point.point_type[1:]}_"
    skip = {f"{prefix}value", "point_exposure_rating"}
    out: list[str] = []
    for key, value in point.props.items():
        if key.startswith(prefix) and key not in skip and value not in ("", None):
            out.append(f"{key[len(prefix) :].replace('_', ' ')}: {value}")
    return "; ".join(out)


def _point_value(point: Any) -> str:
    value = point.value
    if value in ("", None):
        return "-"
    if point.point_type == "Exposure":
        try:
            return rating_text(EXPOSURE_KEY, int(value))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _rating_points(ctx: RunContext) -> list[Block]:
    blocks: list[Block] = []
    for t in ctx.tracks:
        if not t.points:
            continue
        order = {s.uuid: s.position for s in t.segments}
        rows: list[list[Inline]] = []
        for p in sorted(t.points, key=lambda p: order.get(p.segment_uuid, 10**9)):
            seg = t.segment_by_uuid(p.segment_uuid)
            map_url = (
                f"https://www.openstreetmap.org/?mlat={p.lat:.6f}&mlon={p.lon:.6f}"
                f"#map=19/{p.lat:.6f}/{p.lon:.6f}"
            )
            photos = _join([Link(f"photo {i}", u) for i, u in enumerate(p.photos, 1)])
            rows.append(
                [
                    seg.name if seg else (p.segment_name or "?"),
                    p.point_type or "?",
                    _point_value(p),
                    p.remark or "-",
                    _point_details(p) or "-",
                    photos or "-",
                    Link(f"{p.lat:.5f}, {p.lon:.5f}", map_url),
                ]
            )
        if len(ctx.tracks) > 1:
            blocks.append(Para(Strong(t.name)))
        blocks.append(
            Table(["Segment", "Type", "Value", "Remark", "Details", "Photos", "Location"], rows)
        )
    return blocks or [Para("None.")]


def _advisories(ctx: RunContext) -> list[Block]:
    blocks: list[Block] = [
        Heading(2, "Advisories"),
        Para(
            "These are for the reviewer to judge in JOSM. The tool never edits oneway, foot, "
            "bicycle, or any tag other than the two ITRS keys."
        ),
    ]
    for title, lines in (
        ("Existing ITRS values overwritten", _overwritten(ctx)),
        ("Direction", _direction_mismatches(ctx)),
        ("Use and access", _use_mismatches(ctx)),
    ):
        blocks.append(Heading(3, title))
        blocks.append(ItemList(lines) if lines else Para("None."))
    blocks.append(Heading(3, "Rating points"))
    blocks += _rating_points(ctx)
    return blocks


def _review_steps(ctx: RunContext) -> list[Block]:
    if ctx.osm_path:
        first: Inline = ["Open ", Code(ctx.osm_path.name), " in JOSM (File -> Open)."]
    else:
        first = "This was a dry run, so there's no .osm to open. Rerun without --dry-run first."
    steps: list[Inline] = [
        first,
        [
            "Search (Ctrl+F) for ",
            Code("modified"),
            " to select everything this tool changed or created.",
        ],
        "Run the validator (Shift+V) and fix or explain each warning on the selection.",
        "Look at each split listed under Ways: the cut nodes, the tags on each piece, and "
        "the order of the pieces in any relation.",
        "Paste the suggested changeset tags into the upload dialog.",
        "Upload. JOSM sends only the modified and new elements; the rest of the file is "
        "context and is not uploaded.",
    ]
    return [Heading(2, "How to review in JOSM"), ItemList(steps, ordered=True)]


def build_report(ctx: RunContext) -> list[Block]:
    """The whole report as blocks, in order, for any renderer."""
    blocks = _header(ctx) + _summary(ctx) + _changeset(ctx)
    for tm in ctx.matches:
        blocks += _track_section(ctx, tm)
    blocks += _ways_section(ctx)
    blocks += _not_applied(ctx)
    blocks += _advisories(ctx)
    blocks += _review_steps(ctx)
    return blocks


# --- Markdown -----------------------------------------------------------------

_MD_ESCAPE = re.compile(r"([\\`*<|\[\]])|(?<!\w)_|_(?!\w)")


def _md_text(text: str) -> str:
    text = " ".join(text.splitlines())
    return _MD_ESCAPE.sub(lambda m: "\\" + m.group(0), text)


def _md_inline(content: Inline) -> str:
    out: list[str] = []
    for part in inline_parts(content):
        if isinstance(part, Strong):
            out.append(f"**{_md_text(part.text)}**")
        elif isinstance(part, Code):
            text = " ".join(part.text.splitlines())
            fence = "``" if "`" in text else "`"
            pad = " " if fence == "``" else ""
            out.append(f"{fence}{pad}{text}{pad}{fence}")
        elif isinstance(part, Link):
            url = part.url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            out.append(f"[{_md_text(part.text)}]({url})")
        else:
            out.append(_md_text(part))
    return "".join(out)


def render_markdown(blocks: list[Block]) -> str:
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            lines.append(f"{'#' * block.level} {_md_text(block.text)}")
        elif isinstance(block, Para):
            lines.append(_md_inline(block.content))
        elif isinstance(block, ItemList):
            for i, item in enumerate(block.items, 1):
                marker = f"{i}." if block.ordered else "-"
                lines.append(f"{marker} {_md_inline(item)}")
        elif isinstance(block, Table):
            lines.append("| " + " | ".join(_md_text(h) for h in block.header) + " |")
            lines.append("|" + "---|" * len(block.header))
            for row in block.rows:
                lines.append("| " + " | ".join(_md_inline(c) for c in row) + " |")
        elif isinstance(block, CodeBlock):
            lines += ["```", block.text, "```"]
        lines.append("")
    return "\n".join(lines)


def write_markdown(ctx: RunContext, path: Path) -> None:
    """Write the full Markdown report."""
    path.write_text(render_markdown(build_report(ctx)), encoding="utf-8")
