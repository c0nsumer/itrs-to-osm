# itrs-to-osm

Certified ITRS trail raters walk or ride a trail with the ITRS app and get a
GeoJSON export: one line per rated segment, each with a technical difficulty
and an exposure rating. Those trails are usually already in OpenStreetMap,
but the surveyed track never lines up exactly with the OSM way, one way
often spans a dozen segments with different ratings, and copying it all over
by hand means splitting ways and typing tags for an afternoon.

This tool does the mechanical part. It finds the OSM ways under the surveyed
track, splits them where the rating changes, writes the ITRS tags, and hands
you a JOSM file plus a report. You review it in JOSM and upload it yourself.
It never talks to the OSM API.

As of this writing (2026-Oct-12) it handles exports from ITRS app versions
4.4 through 5.3 and writes these two tags:

- `segment_tech_rating` -> `mtb:scale:itrs:technical_difficulty` (1 to 5)
- `segment_max_exposure` -> `mtb:scale:itrs:exposure` (1 to 4)

A value of 0 in the export means "not rated" and is never written. Segments
whose track type is "Paved/surfaced road" are never tagged either, since ITRS
itself doesn't present a rating on surfaced roads. Endurance and wilderness
aren't in the exports, so they're not handled.

## Install

You'll need Python 3.11 or newer. Then:

```
pipx install git+https://github.com/c0nsumer/itrs-to-osm
```

(or `uv tool install git+https://github.com/c0nsumer/itrs-to-osm`, or clone
it and `pip install .` into a venv). That gives you an `itrs-to-osm` command
on macOS, Linux, and Windows.

## Use

Run it on an export, from the directory holding the file:

```
itrs-to-osm "geojson-export_Loam Rider.geojson"
```

That writes two files next to the input: `Loam Rider.osm` (the JOSM file) and
`Loam Rider.report.md` (what changed, what didn't, and why). Add `--html` for
`Loam Rider.report.html`, the same report with a map.

Several exports at once go into one JOSM file, which matters when two tracks
touch the same way (a track's tail often runs a few meters onto the
neighboring trail, and "Pt. 1" of a trail implies a Pt. 2 on the same way).
Separate files would each modify that way from the same base version, and
the second upload would conflict:

```
itrs-to-osm geojson-export_*.geojson --out copper-harbor
```

The first run downloads the area from the Overpass API and caches it under
`./cache`, so reruns are offline and reproducible. `--refresh` downloads
again. `--osm area.osm` skips Overpass and uses a file you saved from JOSM
instead (handy when Overpass is busy, which it often is).

Then open the `.osm` in JOSM:

1. Search for `modified` to select everything the tool changed.
2. Run the validator and look at each split; the report lists every cut and
   whether it snapped to an existing node or inserted one.
3. Paste the suggested changeset comment and source from the report into the
   upload dialog, and upload. Only the modified and new elements go up.

## How it decides

The surveyed track sits a few meters off the OSM way, flickers between ways
at junctions and alongside parallel trails, and sometimes runs onto a
neighboring trail at either end. So per-point "nearest way" is wrong often
enough to matter. Instead the tool decodes the whole track at once (a small
Viterbi), preferring to stay on one way, to switch only at a shared node,
and to favor ways whose name (or whose route relation's name) matches the
track name and whose highway class fits the ITRS track type.

Rating changes almost never fall on an existing OSM node (in the sample
tracks, 3 of 31 were within 15 m of a junction). So a cut snaps to a
junction node within 15 m, else to any node within 5 m, else inserts a new
node on the way's own geometry. The OSM geometry is never moved. Adjacent
segments with the same rating are merged before cutting, and a short tail
onto another way (under 15 m, or badly offset) is dropped and reported
rather than turned into a sliver. Ways are split the way JOSM does it: the
piece with the most nodes keeps the id, every piece gets the original tags,
and parent relations get the pieces inserted in order.

If a way already carries a different ITRS value, the tool refuses to touch
it, says so in the report, and exits with status 2. `--force` overwrites and
records the old value.

All the thresholds are flags (`--corridor`, `--max-offset`, `--snap`,
`--snap-junction`, `--min-piece`), and `--name` or `--way-id` pin the match
when the rater's name and the mapper's name disagree. `--dry-run` matches and
reports without writing a `.osm`.

## What it won't do

It won't upload anything, by design: these edits are small, infrequent, and
nearly always worth a human look, which is also what the OSM Automated Edits
code of conduct asks for. It doesn't edit access or oneway tags either; where
the survey disagrees with OSM about direction or who may use the trail, the
report says so under "Advisories" and leaves the decision to you.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check itrs_to_osm tests
```

The tests run offline on synthesized trails and OSM networks
(`tests/fixtures/builder.py` has eleven scenarios with ground truth, each
modeled on a real case). Real ITRS exports are private and never committed.
If some are present in `research/exports/`, `tests/test_private_samples.py`
runs on them too. `research/README.md` describes that directory.

## Thanks

Mischa Crumbach and ITRS (https://itrs.bike/) for the rating system, the
sample exports, and the data documentation. The OSM wiki pages on the JOSM
file format and the Overpass API, and JOSM's own `SplitWayCommand`, for the
rules this tool copies.

This tool was developed with Claude Code (AI-assisted); the design, review,
and decisions are mine.
