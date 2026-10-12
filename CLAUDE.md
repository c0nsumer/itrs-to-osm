# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

In development, milestone by milestone per `PLAN.md` (the reviewed design; follow it). `project_startup.txt` is the original project brief. Milestones 1-5 are done (scaffold, parser, Overpass/OSM layer, matching, cutting, reports, README) and an independent review's findings are fixed. Next: the user opens the generated `.osm` files in JOSM (milestone 4 acceptance), then release prep. Keep the "Commands" and "Architecture" sections below current as modules land.

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
itrs-to-osm "geojson-export_Loam Rider.geojson" --out-dir out
itrs-to-osm geojson-export_*.geojson --out copper-harbor --out-dir out   # one .osm for a batch
itrs-to-osm export.geojson --osm area.osm --dry-run                        # offline, report only
```

## Architecture

Pipeline (`cli.py` -> `run`): parse exports -> buffered bbox per export, overlapping boxes merged -> Overpass download (or `--osm` file) merged into one `OsmData` -> `match_track` per export -> `apply_matches` for the batch -> `write_josm_file` + report. Exit codes: 0 ok, 1 error, 2 refused (an existing ITRS value differs and no `--force`).

- `itrs.py`: ITRS GeoJSON parser. Field aliases live in the `*_ALIASES` tables; add new names there, never inline. `Track` -> `Segment` / `RatingPoint`. Structural problems raise `ItrsError`; tolerable oddities go to `Track.warnings`. 0 means not rated.
- `overpass.py`: query builder (`highway` ways + their nodes + parent relations, `out meta`), sha256-keyed cache under `./cache`, backoff retry. Never writes to OSM.
- `osm.py`: `Node`/`Way`/`Relation`/`OsmData`, Overpass XML reader, JOSM writer. Metadata round-trips untouched; `mark_modified` sets `action='modify'`; new elements get negative ids from `new_id()`. Mutate `way.nodes` only via `set_way_nodes` (parent index).
- `geo.py`: local projection (same sphere as `itrs.distance_m`), polyline projection/chainage, bbox helpers, name similarity.
- `match.py`: Viterbi decode of track vertices over candidate ways (offset, highway class vs `segment_track_type`, name prior incl. route-relation names, junction-aware switching, chainage-jump penalty). Output: per-segment `Interval`s with projection-independent `WayPos` = (edge index, fraction). Tails dropped by min piece or offset.
- `split.py`: claims -> zones (same-track same-tag claims merge first; longer claim wins overlaps) -> cuts (junction within `--snap-junction`, else node within `--snap`, else inserted node on the way geometry) -> JOSM-style split (most nodes keeps the id, tags copied) -> tags per piece -> relation members inserted next to the original with JOSM's direction logic. Refuses a way whose existing ITRS value differs unless `--force`.
- `tagging.py`: the two keys, value ranges, level names, official ITRS colors, `target_tags` (never for paved roads, never 0).
- `report.py` / `html_report.py`: `RunContext` -> Markdown / HTML (Leaflet map).
- `tests/conftest.py` + `tests/fixtures/builder.py`: synthesize ITRS exports and OSM networks; `SCENARIOS` carries ground truth for eleven edge cases. Never commit a real export. `tests/test_private_samples.py` runs on the gitignored real files and cached downloads when present.
