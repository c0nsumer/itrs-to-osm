"""Command line entry point: ITRS GeoJSON export(s) in, JOSM file and report out.

Nothing here talks to the OSM API. The output is a file for a human to
review in JOSM and upload themselves.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .geo import buffer_bbox, merge_bboxes
from .itrs import ItrsError, Track, flag_reexports, load_track
from .match import MatchOptions, match_track
from .osm import Bounds, OsmData, read_osm_file, read_osm_xml, write_josm_file
from .overpass import DEFAULT_ENDPOINT, OverpassError, fetch_area
from .points import PointOptions, apply_points
from .report import RunContext, large_edit_warning, write_markdown
from .split import CutOptions, apply_matches

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

AREA_BUFFER_M = 100.0
INPUT_PREFIX = "geojson-export_"

log = logging.getLogger("itrs_to_osm")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="itrs-to-osm",
        description=(
            "Copy ITRS trail ratings from a rater's GeoJSON export onto the matching "
            "OpenStreetMap ways, and write a JOSM file plus a report for human review. "
            "Nothing is uploaded to OSM."
        ),
    )
    p.add_argument("geojson", nargs="+", type=Path, help="ITRS GeoJSON export(s)")
    p.add_argument("--out", help="base name for the combined .osm and report")
    p.add_argument("--out-dir", type=Path, help="output directory (default: next to the input)")
    p.add_argument("--osm", type=Path, help="use this local .osm file instead of querying Overpass")
    p.add_argument("--overpass-url", default=DEFAULT_ENDPOINT, help="alternate Overpass endpoint")
    p.add_argument(
        "--cache-dir", type=Path, default=Path("cache"), help="Overpass response cache (./cache)"
    )
    p.add_argument("--refresh", action="store_true", help="ignore cached Overpass responses")
    p.add_argument("--name", help="pin matching to ways/relations with this OSM name")
    p.add_argument("--way-id", help="restrict matching to these way ids (comma separated)")
    p.add_argument("--corridor", type=float, default=25.0, help="candidate corridor, m (25)")
    p.add_argument("--max-offset", type=float, default=15.0, help="max 90th pct offset, m (15)")
    p.add_argument("--snap", type=float, default=5.0, help="snap a cut to any node within, m (5)")
    p.add_argument(
        "--snap-junction", type=float, default=15.0, help="snap a cut to a junction within, m (15)"
    )
    p.add_argument("--min-piece", type=float, default=15.0, help="drop pieces shorter than, m (15)")
    p.add_argument("--force", action="store_true", help="overwrite differing existing ITRS values")
    p.add_argument(
        "--exposure-min",
        type=int,
        default=2,
        help="tag a node for Exposure points rated this or worse, 1-4 (2: everything ITRS exports)",
    )
    p.add_argument(
        "--no-exposure-nodes", action="store_true", help="write no nodes for Exposure points"
    )
    p.add_argument("--html", action="store_true", help="also write an HTML report")
    p.add_argument("--dry-run", action="store_true", help="match and report, write no .osm")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def output_base(args: argparse.Namespace) -> Path:
    """Directory and base name for the outputs."""
    first = args.geojson[0]
    out_dir = args.out_dir or first.parent
    if args.out:
        base = args.out
    elif len(args.geojson) == 1:
        base = first.stem
        if base.startswith(INPUT_PREFIX):
            base = base[len(INPUT_PREFIX) :]
    else:
        raise SystemExit("error: --out NAME is required when processing several files")
    return out_dir / base


def load_area(tracks: list[Track], args: argparse.Namespace) -> tuple[OsmData, str, str | None]:
    """The OSM data covering every track: (data, source description, osm_base)."""
    if args.osm:
        data = read_osm_file(args.osm)
        return data, str(args.osm), data.osm_base
    data = OsmData()
    boxes = merge_bboxes([buffer_bbox(t.bbox(), AREA_BUFFER_M) for t in tracks])
    for box in boxes:
        log.info("Downloading area %s", ", ".join(f"{v:.5f}" for v in box))
        xml = fetch_area(
            box, cache_dir=args.cache_dir, refresh=args.refresh, endpoint=args.overpass_url
        )
        part = read_osm_xml(xml)
        part.bounds.append(
            Bounds(
                minlat=box[1], minlon=box[0], maxlat=box[3], maxlon=box[2], origin="Overpass API"
            )
        )
        data.merge(part)
    return data, f"Overpass API ({args.overpass_url})", data.osm_base


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    try:
        base = output_base(args)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return EXIT_ERROR

    tracks: list[Track] = []
    for path in args.geojson:
        try:
            tracks.append(load_track(path))
        except ItrsError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
    flag_reexports(tracks)
    for t in tracks:
        for w in t.warnings:
            log.warning("%s: %s", t.name, w)

    try:
        data, osm_source, osm_base = load_area(tracks, args)
    except (OverpassError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR

    way_ids = None
    if args.way_id:
        try:
            way_ids = {int(x) for x in args.way_id.split(",") if x.strip()}
        except ValueError:
            print(f"error: --way-id expects comma separated integers, got {args.way_id!r}")
            return EXIT_ERROR
    match_options = MatchOptions(
        corridor_m=args.corridor,
        max_offset_m=args.max_offset,
        min_piece_m=args.min_piece,
        name_hint=args.name,
        way_ids=way_ids,
    )
    cut_options = CutOptions(
        snap_m=args.snap,
        snap_junction_m=args.snap_junction,
        min_piece_m=args.min_piece,
        force=args.force,
    )

    if args.exposure_min not in range(1, 5):
        print(f"error: --exposure-min expects 1 to 4, got {args.exposure_min}", file=sys.stderr)
        return EXIT_ERROR
    point_options = PointOptions(
        snap_m=args.snap,
        min_exposure=args.exposure_min,
        max_offset_m=args.max_offset,
        force=args.force,
        enabled=not args.no_exposure_nodes,
    )

    matches = [match_track(t, data, match_options) for t in tracks]
    result = apply_matches(data, matches, cut_options)
    points = apply_points(data, matches, result, point_options)

    base.parent.mkdir(parents=True, exist_ok=True)
    osm_path: Path | None = None
    if not args.dry_run:
        osm_path = base.with_name(base.name + ".osm")
        write_josm_file(data, osm_path, generator=f"itrs-to-osm/{__version__}")

    ctx = RunContext(
        tracks=tracks,
        matches=matches,
        result=result,
        points=points,
        data=data,
        match_options=match_options,
        cut_options=cut_options,
        point_options=point_options,
        osm_source=osm_source,
        osm_base=osm_base,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tool_version=__version__,
        osm_path=osm_path,
        warnings=[w for tm in matches for w in tm.warnings],
    )
    report_path = base.with_name(base.name + ".report.md")
    write_markdown(ctx, report_path)
    if args.html:
        from .html_report import write_html

        write_html(ctx, base.with_name(base.name + ".report.html"))

    print(summary_line(ctx, report_path))
    large = large_edit_warning(ctx)
    if large:
        print(f"warning: {large}", file=sys.stderr)
    refused = len(result.refusals) + len(points.refusals)
    if refused and not args.force:
        print(
            f"refused: {refused} existing ITRS value(s) differ; "
            "see the report, or rerun with --force to overwrite",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return EXIT_OK


def summary_line(ctx: RunContext, report_path: Path) -> str:
    segs = [sm for tm in ctx.matches for sm in tm.segments]
    counts = {
        "matched": sum(sm.status == "matched" for sm in segs),
        "partial": sum(sm.status == "partial" for sm in segs),
        "unmatched": sum(sm.status == "unmatched" for sm in segs),
    }
    ways = ctx.result
    parts = [
        f"{len(ctx.tracks)} track(s), {len(segs)} segments: "
        f"{counts['matched']} matched, {counts['partial']} partial, {counts['unmatched']} unmatched",
        f"{len(ways.modified_ways)} way(s) modified, {ways.ways_added} created by splits, "
        f"{ways.nodes_added + ctx.points.nodes_added} node(s) added, "
        f"{ways.relations_modified} relation(s) updated",
        f"{len(ctx.points.written)} exposure node(s) tagged",
    ]
    if ctx.osm_path:
        parts.append(f"wrote {ctx.osm_path}")
    parts.append(f"report {report_path}")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
