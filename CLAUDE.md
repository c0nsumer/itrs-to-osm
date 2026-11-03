# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

In development, milestone by milestone per `PLAN.md` (the reviewed design; follow it). `research/project_startup.txt` is the original project brief (private, gitignored). Milestones 1-7 are done (scaffold, parser, Overpass/OSM layer, matching, cutting, Markdown and HTML reports, README, exposure points as tagged nodes), an independent review's findings are fixed, and the user has made real OSM edits with the output (2026-Oct-12). 2026-Nov-03: re-survey handling (newer survey wins overlaps, stale points, re-export warning, combine advisory) built against the second Copper Harbor batch (`research/exports/ch_new/`, 35 exports with overlapping "update" tracks). Next: incorporate feedback from ITRS and the Copper Harbor Trails Club, then release prep (milestone 8) when the user says so. Keep the "Commands" and "Architecture" sections below current as modules land.

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

# Run the tool (writes <name>.osm and <name>.report.md next to the input; --html adds a map page)
itrs-to-osm "research/exports/geojson-export_Loam Rider.geojson" --out-dir research/output
itrs-to-osm research/exports/*.geojson --out copper-harbor --out-dir research/output   # one .osm for a batch
itrs-to-osm export.geojson --osm area.osm --dry-run                        # offline, report only
```

## Architecture

Pipeline (`cli.py` -> `run`): parse exports -> buffered bbox per export, overlapping boxes merged -> Overpass download (or `--osm` file) merged into one `OsmData` -> `match_track` per export -> `apply_matches` for the batch -> `apply_points` (exposure nodes) -> `write_josm_file` + report. Exit codes: 0 ok, 1 error, 2 refused (an existing ITRS value differs and no `--force`).

- `itrs.py`: ITRS GeoJSON parser. Field aliases live in the `*_ALIASES` tables; add new names there, never inline. `Track` -> `Segment` / `RatingPoint`. Names are trimmed. Structural problems raise `ItrsError`; tolerable oddities go to `Track.warnings`. 0 means not rated. `flag_reexports` warns when a batch holds one survey twice under different uuids (same segments, ratings, dates, vertices).
- `overpass.py`: query builder (`highway` ways + their nodes + parent relations, `out meta`), sha256-keyed cache under `./cache`, backoff retry. Never writes to OSM.
- `osm.py`: `Node`/`Way`/`Relation`/`OsmData`, Overpass XML reader, JOSM writer. Metadata round-trips untouched; `mark_modified` sets `action='modify'`; new elements get negative ids from `new_id()`. Mutate `way.nodes` only via `set_way_nodes` (parent index).
- `geo.py`: local projection (same sphere as `itrs.distance_m`), polyline projection/chainage, bbox helpers, name similarity.
- `match.py`: Viterbi decode of track vertices over candidate ways (offset, highway class vs `segment_track_type`, name prior incl. route-relation names, junction-aware switching, chainage-jump penalty). Output: per-segment `Interval`s with projection-independent `WayPos` = (edge index, fraction), plus `vertex_places` (way, chainage, offset per track vertex) for the points step. Tails dropped by min piece or offset.
- `split.py`: claims -> zones (same-track same-tag claims merge first; where ratings differ the newer survey wins, then the longer claim; each overlap recorded as `Overlap` on `WayChange.overlaps`; `Piece.surveyed` carries the winning date) -> cuts (junction within `--snap-junction`, else node within `--snap`, else inserted node on the way geometry) -> JOSM-style split (most nodes keeps the id, tags copied) -> tags per piece -> relation members inserted next to the original with JOSM's direction logic. An existing ITRS value that differs is replaced when the survey timestamp is later than the way's last OSM edit (`survey_newer_than_edit`, noted in the report), else refused unless `--force`. Same rule for exposure nodes in `points.py`.
- `points.py`: after `apply_matches`. Each Exposure point -> its segment vertex -> `vertex_places` (clamped into the interval within `--snap`) -> the piece holding that chainage -> an existing node within `--snap` (never a junction or a node with other tags) else `insert_node_at` -> `mtb:scale:itrs:exposure` on the node. A point whose piece a newer survey rated is stale and skipped; two points on one node: newer survey wins, then higher rating. Same refuse/`--force` policy as ways; `--exposure-min` (default 2), `--no-exposure-nodes`. Other point types stay report-only.
- `tagging.py`: the two keys, value ranges, level names, official ITRS colors, `target_tags` (never for paved roads, never 0).
- `report.py` / `html_report.py`: `RunContext` -> Markdown / HTML (Leaflet map). Advisories include "Overlapping surveys" and "Pieces that could be combined" (adjacent same-tag pieces after an update; the tool never combines ways).
- `tests/conftest.py` + `tests/fixtures/builder.py`: synthesize ITRS exports and OSM networks; `SCENARIOS` carries ground truth for twelve edge cases; `tests/test_resurvey.py` builds overlapping dated tracks on top. Never commit a real export. `tests/test_private_samples.py` runs on the gitignored real exports in `research/exports/` (top level only) and cached downloads when present.
- `research/`: local-only private material (real exports, ITRS docs and QGIS assets, the brief, run output). Gitignored except `research/README.md`; never commit anything else from it.
