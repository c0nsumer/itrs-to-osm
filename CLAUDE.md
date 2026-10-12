# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

In development, milestone by milestone per `PLAN.md` (the reviewed design; follow it). `project_startup.txt` is the original project brief. Milestone 1 (scaffold, ITRS parser) is done. Keep the "Commands" and "Architecture" sections below current as modules land.

## Commands

Run from the repo root with the venv active (`source .venv/bin/activate`), or prefix with `.venv/bin/`.

```bash
# One-time setup
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# Tests (offline; the real-sample tests skip when the private files are absent)
pytest

# Lint and format; both must be clean before committing
ruff check itrs_to_osm tests
ruff format itrs_to_osm tests

# Run the tool
itrs-to-osm "geojson-export_Loam Rider.geojson"
```

## Architecture

- `itrs_to_osm/itrs.py`: ITRS GeoJSON parser. Field aliases live in the `*_ALIASES` tables; add new names there, never inline. Produces `Track` -> `Segment` / `RatingPoint` dataclasses. Structural problems raise `ItrsError`; tolerable oddities go to `Track.warnings`.
- `itrs_to_osm/cli.py`: argparse entry point (`itrs-to-osm`). All flags from the plan are declared; later milestones wire them up. Exit codes: 0 ok, 1 error, 2 refused (existing ITRS value differs, no `--force`).
- `tests/conftest.py`: helpers that synthesize ITRS-shaped GeoJSON in memory (`make_export`, `segment_feature`, `point_feature`, `positions`). Use these rather than committing any real export.
- `tests/test_private_samples.py`: runs against `geojson-export_*.geojson` in the repo root or `tests/private/` when present, skips otherwise.

## What this will be

A cross-platform (macOS, Linux, Windows) CLI that takes an ITRS (International Trail Rating System, https://itrs.bike/) GeoJSON export from a trail rater's app and copies its ratings onto the matching OpenStreetMap ways. Planned flow: find the GeoJSON's area -> download it via Overpass to OSM XML -> match ITRS segments to OSM ways (the surveyed track won't align exactly with OSM geometry, but should be close) -> set/overwrite ITRS tags, splitting ways where needed to preserve per-segment granularity -> write a JOSM-openable OSM XML plus a detailed report of changes and of ITRS data that couldn't be applied (Markdown/HTML; a rendered overview is a stretch goal).

Hard constraints:
- Never upload or commit anything to OSM. Output is a file for human review in JOSM (per the OSM Automated Edits code of conduct).
- `project_startup.txt`, `itrs_osm_schema.csv`, `ITRS rating data explanation.pdf`, the `ITRS QGIS Assets-*.zip` (ITRS's own QGIS styles, palette, icons, and scripts), and the `geojson-export_*.geojson` samples are private reference material. Keep them locally but out of the repo (it will be public as c0nsumer/itrs-to-osm and squashed for v1.0.0). `.gitignore` already excludes them.
- A future web service version and the OSM wiki page are out of scope for now. So is integration with `~/projects/trailmaps.app-map-generator` (which currently uses `mtb:scale:imba`); it may be cited in plans only.

## Tag mapping

Only two tags are populated; the GeoJSONs carry no endurance or wilderness data, so don't handle `mtb:scale:itrs:endurance` or `mtb:scale:itrs:wilderness`.

- `segment_tech_rating` -> `mtb:scale:itrs:technical_difficulty`
- `segment_max_exposure` -> `mtb:scale:itrs:exposure`

`itrs_osm_schema.csv` defines value meanings (1-5, modeled on `mtb:scale:imba`). Two known corrections are pending in it:
- The header cell says `mb:scale:itrs:*`. It must be `mtb:scale:itrs:*`.
- The schema lacks a 0 value, but ITRS uses 0 to mean "Not rated" (`ITRS rating data explanation.pdf`, p. 9: 0 = Not rated, 1 = Green, 2 = Blue, 3 = Red, 4 = Black, 5 = Orange). Never write 0 to an OSM tag: skip that key, leave any existing ITRS tag for it alone, and list the segment as not rated in the report. Loam Rider has `segment_max_exposure: 0` on every segment, which is this case. Valid written values: technical difficulty 1-5, exposure 1-4.
- Segments with `segment_track_type: "Paved/surfaced road"` are never tagged, whatever their rating (user decision, 2026-Oct-12). Match them and list them in the report as not applied. "Unsurfaced road/double track" and "Single trail" segments are tagged normally.

## ITRS GeoJSON format (observed in the samples)

- A FeatureCollection containing one `Point` with `point_type: "Start"` (track-level summary: name, uuid, length, max ratings, tech-level share percentages), then one `LineString` per rated segment (3D coordinates, lon/lat/elevation), plus optional `Point`s with `point_type` of `Exposure` or `GeneralAssessmentRemark` that reference a segment via `point_segment_uuid`.
- Segments are ordered by `segment_position_in_trail` and named `S001`, `S002`, etc. They also carry `segment_itrs_version`, `segment_direction`, `segment_track_type`, `segment_use`, and similar attributes.
- Field names vary by ITRS version, and the parser must handle both forms. The 4.4 export (Visp) uses `segment_trail_name`/`segment_trail_uuid` where the 5.x exports use `segment_track_name`/`segment_track_uuid`. The 4.4 file also lacks the top-level `version`/`date` keys. Point features use `point_track_*` in both versions. Expect other renames in future ITRS versions, so map field aliases in one place rather than scattering version checks.
- Three samples are from Copper Harbor, Michigan; one (Visp) is from Switzerland. All four have corresponding ways in OSM, so they're usable as real test cases. Matching edge cases need synthesized test files.
