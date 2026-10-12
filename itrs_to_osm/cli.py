"""Command line entry point.

Milestone 1: parse the ITRS exports and print a summary. Later milestones
wire in the Overpass download, matching, splitting, tagging, and output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .itrs import ItrsError, Track, load_track

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2


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
    p.add_argument("--overpass-url", help="alternate Overpass API endpoint")
    p.add_argument("--cache-dir", type=Path, help="Overpass response cache (default: ./cache)")
    p.add_argument("--refresh", action="store_true", help="ignore cached Overpass responses")
    p.add_argument("--name", help="pin matching to ways/relations with this OSM name")
    p.add_argument("--way-id", help="restrict matching to these way ids (comma separated)")
    p.add_argument("--corridor", type=float, default=25.0, help="candidate corridor, m (25)")
    p.add_argument("--max-offset", type=float, default=15.0, help="max 90th pct offset, m (15)")
    p.add_argument("--snap", type=float, default=5.0, help="snap cut to any node within, m (5)")
    p.add_argument(
        "--snap-junction", type=float, default=15.0, help="snap cut to a junction within, m (15)"
    )
    p.add_argument("--min-piece", type=float, default=15.0, help="drop pieces shorter than, m (15)")
    p.add_argument("--force", action="store_true", help="overwrite differing existing ITRS values")
    p.add_argument("--html", action="store_true", help="also write an HTML report")
    p.add_argument("--dry-run", action="store_true", help="match and report, write no .osm")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def summarize(track: Track) -> str:
    lines = [
        f"{track.name} ({track.source})",
        f"  track uuid {track.uuid or '?'}; file version {track.file_version or '?'}; "
        f"export date {track.export_date or '?'}; ITRS {', '.join(track.itrs_versions) or '?'}",
        f"  {len(track.segments)} segments, {len(track.points)} rating points, "
        f"{track.length_m or 0:.0f} m; survey dates {', '.join(track.survey_dates) or '?'}",
    ]
    for seg in track.segments:
        tech = seg.tech_rating if seg.tech_rated else "not rated"
        exp = seg.max_exposure if seg.exposure_rated else "not rated"
        lines.append(
            f"  {seg.name}: tech {tech}, exposure {exp}, {seg.track_type}, "
            f"{seg.use}, {seg.direction}, {seg.length_m or seg.geometry_length_m():.0f} m"
        )
    for w in track.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tracks: list[Track] = []
    for path in args.geojson:
        try:
            tracks.append(load_track(path))
        except ItrsError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_ERROR
    for track in tracks:
        print(summarize(track))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
