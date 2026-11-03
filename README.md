# itrs-to-osm

Certified ITRS trail raters walk or ride a trail with the ITRS app and get a
GeoJSON export: one line per rated segment, each with a technical difficulty
and an exposure rating. Those trails are usually already in OpenStreetMap,
but the surveyed track never lines up exactly with the OSM way, one way
often spans a dozen segments with different ratings, and copying it all over
by hand means splitting ways and typing tags for an afternoon.

This tool does the mechanical part without doing mechanical edits. It finds the OSM ways under the surveyed
track, splits them where the rating changes, writes the ITRS tags, and hands
you a JOSM file plus a report. You (a human) reviews the output, the data in in JOSM and then uploads it.
This keeps a human in the loop.

As of this writing (2026-Nov-02) it handles exports from ITRS app versions
4.4 through 5.3 and writes these two tags on ways:

- `segment_tech_rating` -> `mtb:scale:itrs:technical_difficulty` (1 to 5)
- `segment_max_exposure` -> `mtb:scale:itrs:exposure` (1 to 4)

It also writes one tag on nodes. An ITRS export carries an Exposure point
for each exposed section rated blue or worse (the section that gives the
segment its exposure rating). The tool tags a node of the matched way at
each one:

- `point_exposure_rating` -> `mtb:scale:itrs:exposure` (1 to 4) on a node

So the way says how exposed the worst spot along it is, and the node says
where that spot is. Nothing else from the point goes into OSM; the length,
trail bed width, off-camber, and photo links stay in the report.

A value of 0 in the export means "not rated" and is never written. Segments
whose track type is "Paved/surfaced road" are never tagged either, since ITRS
itself doesn't present a rating on surfaced roads. Endurance and wilderness
aren't in the exports, so they're not handled. Neither are the other point
types (water, river crossings, rockfall, wildlife, general remarks); they're
listed in the report only.

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
the second upload would conflict. It also matters when one export updates
part of another; see "Re-surveys and updates" below:

```
itrs-to-osm geojson-export_*.geojson --out copper-harbor
```

A run goes in this order, whether it holds one export or forty: every export
is parsed first, then one OSM download covers all of them (areas that
overlap merge into one request), each track is matched to ways on its own,
then every track's claims on each way are resolved together (where ratings
differ, the newer survey wins, then the longer claim), the ways are cut and
tagged, the Exposure points get their nodes, and the report is written. So
the order of the files on the command line doesn't matter, and a track
matched in one run comes out the same in a batch.

The first run downloads the area from the Overpass API and caches it under
`./cache`, so reruns are offline and reproducible. `--refresh` downloads
again. `--osm area.osm` skips Overpass and uses a file you saved from JOSM
instead (handy when Overpass is busy, which it often is).

Then open the `.osm` in JOSM:

1. Search for `modified` to select everything the tool changed.
2. Run the validator and look at each split; the report lists every cut and
   whether it snapped to an existing node or inserted one.
3. Look at each exposure node; the report lists where each one landed and
   how far it is from the surveyed point.
4. Paste the suggested changeset comment and source from the report into the
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

Exposure points sit on a vertex of the surveyed track, and the decode has
already put that vertex on a way, so the node goes where the match says,
not on the nearest way to the point. An existing node within 5 m gets the
tag, unless it's shared with another trail (which trail would it belong to?)
or already carries other tags such as a gate. Otherwise a new node is
inserted on the way's own geometry. In the sample tracks, 28 of 30 points
landed on an existing node.

If a way or node already carries a different ITRS value, the tool refuses
to touch it, says so in the report, and exits with status 2. `--force`
overwrites and records the old value (the next section has the workflow).
One thing it can't do is remove a node for a point that ITRS no longer
exports, so the report asks you to look for stale ones.

All the thresholds are flags (`--corridor`, `--max-offset`, `--snap`,
`--snap-junction`, `--min-piece`), and `--name` or `--way-id` pin the match
when the rater's name and the mapper's name disagree. `--exposure-min`
raises the rating a point needs before it gets a node (the default, 2, is
also ITRS's export threshold, so everything exported is written), and
`--no-exposure-nodes` turns the node step off. `--dry-run` matches and
reports without writing a `.osm`.

## Re-surveys and updates

ITRS can't edit a segment in place. When a rater re-rates a corner, or
re-surveys part of a trail after work on it, the fix arrives as a short new
export that overlaps the original ("Berry Picker" plus "Berry Picker (first
corner update)" at Copper Harbor). Long trails also arrive in parts
("Garden Brook Part 1", "Pt. 2", "Part 3"), and the parts overlap by a few
meters where one survey ended and the next began.

Where two tracks in a batch rate the same stretch of a way differently, the
newer survey wins (each segment carries a `segment_last_mutated_on_utc`
date). Between surveys of the same date, the longer claim wins, which is
what you want at a junction where one track's tail runs a few meters onto
the other's trail. Every such overlap is listed in the report under
"Overlapping surveys" with both ratings, both dates, and which one won, so
check that list first when a batch holds updates. The older survey's
Exposure points on a re-rated stretch are stale (the newer survey exported
its own) and get no node; the report says so.

If the batch holds the same survey twice (ITRS can export one track under
two names and uuids; "Red Trail" and "Red Trail Sync #2" were identical),
the report warns that the second is a re-export. Both are processed and the
results coincide, so drop one of the files and rerun if you'd rather have a
tidy report.

Updating ratings that are already in OSM is the same run, against the
current OSM data: give the tool the new export (alone, or with the rest of
the batch). Where the new rating differs from what a way or node already
carries, the tool refuses and exits with status 2. That's deliberate, as it
can't tell a re-survey from a mistake: OSM carries no survey date for these
keys. Read the refusals in the report, and if they're the update you expect,
rerun with `--force`. The tool then cuts the way at the new boundaries,
tags the pieces, and records every overwritten value under "Existing ITRS
values overwritten".

The cuts from the earlier survey stay where they are, so after an update
two adjacent pieces can end up with identical tags. The tool doesn't
combine ways (it can't tell a cut it made last time from one somebody else
needed, and combining deletes a way), but the report lists those pairs
under "Pieces that could be combined". If nothing else needs the cut,
select both ways in JOSM and use Tools -> Combine Way before uploading.

Two things the tool leaves alone on an update: a stretch the new survey
rates 0 (not assessed) or marks as paved keeps whatever tags it had, since
0 means "not rated" rather than "no rating", and the report shows it under
"Not applied"; and nodes from an earlier run whose points the new survey
no longer exports, which you'll want to find and clear by hand.

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
(`tests/fixtures/builder.py` has twelve scenarios with ground truth, each
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
