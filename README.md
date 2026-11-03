# itrs-to-osm

itrs-to-osm is a utility tool to help bridge data between the [International
Trail Rating System (ITRS)](https://itrs.bike/) survey app and OpenStreetMap
(OSM). It is a niche tool to be used by a trail mapper who has obtained an
ITRS GeoJSON and wishes to add the trail ratings to an existing OSM way. It
does not create new ways.

Because ITRS surveys result in a new line, it usually won't match the OSM way
exactly. This tool uses multivariable matching to the OSM way, then splitting
it (as needed) to appropriately apply the ITRS tags to each portion. This has
a significant time savings over manually adding the ITRS tags.

This tool makes no changes to OSM itself. It downloads the current OSM data
from Overpass, adjusts it based on the ITRS data, and outputs a `.osm` file
that the mapper opens in [JOSM](https://josm.openstreetmap.de/), reviews, and
uploads. By having a human review it avoids many of the mechanical edit
pitfalls while providing a significant automation benefit.

As of v0.3.0 it handles GeoJSON exports from ITRS app versions 4.4 through 5.3
and writes these two tags on ways:

- `segment_tech_rating` -> [`mtb:scale:itrs:technical_difficulty`](https://wiki.openstreetmap.org/wiki/Key:mtb:scale:itrs:technical_difficulty) (1 to 5)
- `segment_max_exposure` -> [`mtb:scale:itrs:exposure`](https://wiki.openstreetmap.org/wiki/Key:mtb:scale:itrs:exposure) (1 to 4)

It also writes one tag on nodes:

- `point_exposure_rating` -> [`mtb:scale:itrs:exposure`](https://wiki.openstreetmap.org/wiki/Key:mtb:scale:itrs:exposure) (1 to 4) on a node

No other data is copied from ITRS to OSM. The rest of a point (length, trail
bed width, off-camber, and photo links) stays in the report.

A value of 0 in the ITRS GeoJSON means "not rated" and thus is never
translated to OSM. Segments whose track type is "Paved/surfaced road" are
never tagged either, since ITRS itself doesn't present a rating on surfaced
roads.

(In the future it may be extended to handle other ITRS points such as Water
fountains, River Crossings, Rock Fall Zones, Wildlife, or other general
annotations. These are listed in the output report, but not processed.)

## Install

Python 3.11 or newer, then:

```
pipx install git+https://github.com/c0nsumer/itrs-to-osm
```

`uv tool install git+https://github.com/c0nsumer/itrs-to-osm` works too, as
does a clone and `pip install .` in a venv. Each gives you an `itrs-to-osm`
command on macOS, Linux, and Windows.

## Use

One export, run from the directory holding the file:

```
itrs-to-osm "geojson-export_Loam Rider.geojson"
```

This writes `Loam Rider.osm` (the JOSM file) and `Loam Rider.report.md` (what
changed, what didn't, and why) next to the input. `--html` adds `Loam
Rider.report.html`, the same report with a map.

Several exports go into one JOSM file:

```
itrs-to-osm geojson-export_*.geojson --out copper-harbor
```

Use one file per batch whenever two tracks can touch the same way (a tail onto
the neighboring trail, "Pt. 1" and "Pt. 2" of one trail, an update that
overlaps its original). Separate files would each edit that way from the same
base version, and the second upload would conflict.

A run goes in this order, for one export or forty:

1. Parse every export.
2. Download the OSM data once (overlapping areas merge into one request).
3. Match each track to ways on its own.
4. Resolve every track's claims on each way together. Where ratings differ,
   the newer survey wins, then the longer claim.
5. Cut and tag the ways, place the Exposure nodes, write the report.

So the order of the files on the command line doesn't matter.

The first run downloads from the Overpass API and caches under `./cache`, so
reruns are offline and reproducible. `--refresh` downloads again. `--osm
area.osm` uses a file saved from JOSM instead (handy when Overpass is busy,
which it often is).

Suggested workflow begins with opening the `.osm` in JOSM:

1. Search for `modified` to select everything the tool changed.
2. Run JOSM's validator on that selection and fix or explain each warning.
3. Check each split against the report's "OSM ways changed" section, which
   lists every cut and whether it snapped to an existing node or inserted
   one.
4. Check each exposure node. The report's "OSM nodes changed" section has a
   row per ITRS Exposure point: the OSM node that got the tag (existing or
   new), the way it sits on, and the distance from the surveyed point to
   that node. The node should sit on the trail at the exposed spot, not on
   a junction.
5. Paste the changeset comment and source from the end of the report into
   the upload dialog, and upload. Only the modified and new elements go up.

## How it decides

The short version: a rater's track is a GNSS recording, and GNSS recordings
wander. On a wooded trail the line sits a few meters to one side of the
real trail, drifts further under cliffs and tree cover, and at a junction
can look like it went down the wrong fork for a few meters. The OSM way is
the better geometry (it's been mapped and refined already), so the tool
never moves it. Instead it works out which OSM ways the track followed, where
along those ways each ITRS rating starts and stops, and writes the ratings
there. The recorded line is only used for that matching; no part of it is
written to OSM. Everything the tool decides is listed in the report, and the
thresholds below are the ones it uses when the data is ambiguous.

**Matching.** Because of that wander, picking the nearest way for each
point of the track doesn't work: the choice flips between parallel trails,
jumps to the crossing trail at every junction, and lands on the neighbor
wherever the rater overshot the end. So the tool decodes the whole track at
once (a small Viterbi, the same idea GPS map-matching uses). It finds the
path through the OSM network that best explains the recording as a whole,
preferring to stay on one way, to switch only at a node the two ways share,
and to favor ways whose name (or route relation name) matches the track and
whose highway class fits the ITRS track type. A single stray fix can't pull
the match onto another trail, because the rest of the track still argues
for the right one.

**Cuts.** A rating change almost never falls on an existing node (3 of 31 in
the sample tracks were within 15 m of a junction). A cut snaps to a junction
node within 15 m, else to any node within 5 m, else inserts a node on the
way's own geometry. Geometry is never moved. Adjacent segments with the same
rating merge before cutting. A tail onto another way shorter than 15 m, or
badly offset, is dropped and reported rather than turned into a sliver. Splits
follow JOSM's rules: the piece with the most nodes keeps the id, every piece
keeps the original tags, and parent relations get the pieces in order.

**Exposure nodes.** A point sits on a vertex of the surveyed track, and the
decode has already put that vertex on a way, so the node goes where the match
says, not on the nearest way to the point. An existing node within 5 m gets
the tag unless it's a junction (which trail would it belong to?) or carries
other tags, such as a gate. Otherwise a node is inserted. In the sample
tracks, 28 of 30 points landed on an existing node.

**Values already in OSM.** Between two exports in the same run, the newer
survey wins (see "Re-surveys and updates"). A value already in OSM carries
no survey date, only the date of the edit that put it there, so the tool
compares the export's survey time with the element's last OSM edit. If the
survey is newer than that edit, the value in OSM came from an older survey,
and the tool replaces it and says so in the report. If the element was
edited after the survey, there's no telling which is newer, so the tool
refuses, says so in the report, and exits with status 2. `--force`
overwrites those too and records the old value. The tool can't remove a
node for a point ITRS no longer exports, so the report asks you to look for
stale ones.

**Flags.** `--corridor`, `--max-offset`, `--snap`, `--snap-junction`, and
`--min-piece` set the thresholds above. `--name` or `--way-id` pin the match
when the rater's and the mapper's names disagree. `--exposure-min` raises the
rating a point needs for a node (the default, 2, is ITRS's own export
threshold). `--no-exposure-nodes` turns the node step off. `--dry-run` matches
and reports without writing a `.osm`.

## Re-surveys and updates

ITRS can't edit a segment in place. A re-rated corner arrives as a short new
export that overlaps the original ("Berry Picker" plus "Berry Picker (first
corner update)"), long trails arrive in parts that overlap a few meters at the
joins, and one survey can turn up twice under two names. The rules for all of
that:

- **The newer survey wins** where two exports in the same run rate the
  same stretch differently, by each segment's `segment_last_mutated_on_utc`
  timestamp, so an afternoon re-rating beats the morning's full run. Equal
  timestamps fall back to the longer claim, which is right for a tail onto
  a neighbor. Every overlap is listed under "Overlapping surveys" with both
  ratings, both dates, and the winner. Read that list first. (A value
  already in OSM is compared with the element's last OSM edit instead;
  see below.)
- **Exposure points follow suit.** An older survey's point on a spot a newer
  track also covers is stale and gets no node, even when the two agree on
  the way's rating: the newer survey exported its own points there. Stale
  points are listed under "Not applied". Older points beyond the newer
  track's reach are kept.
- **Reruns over already-split ways** work. A short segment at a piece
  boundary now matches as a tail onto the neighboring piece; its point still
  gets a node there when that piece is the same trail (a way named like one
  the track kept), and stays unplaced when it's a different trail (a
  junction overshoot). A point the match can't place at all is looked up by
  location: an existing exposure node within the snap distance counts as its
  node, "already tagged (found by location ...)", or is refused if the value
  differs.
- **A node rated worse than its way** can happen when the point's own
  segment didn't land there (a sliver dropped after an overlap, a short gap
  closed from the neighbor). ITRS rates a way as its worst section, so it
  shouldn't. Each case is listed under "Exposure nodes above their way's
  rating" for you to settle.
- **A re-export** (same segments, ratings, timestamps, and vertices; only the
  uuids differ, as with "Red Trail" and "Red Trail Sync #2") gets a warning.
  Both files are processed and the results coincide, so drop one and rerun
  for a tidier report.

Updating ratings already in OSM is the same run against current OSM data.
Where the new rating differs from what a way or node carries, the tool
checks the survey time against the element's last OSM edit. A survey newer
than the edit replaces the value, since whatever put that value there came
from an older survey. An element edited after the survey is refused with
exit status 2, since it can't tell a re-survey from a mistake. If the
refusals in the report are the update you expect, rerun with `--force`.
Either way the tool cuts at the new boundaries, tags the pieces, and
records every overwritten value under "Existing ITRS values overwritten".

Cuts from the earlier survey stay, so after an update two adjacent pieces can
carry identical tags. The tool never combines ways (it can't tell its own old
cut from one somebody else needed, and combining deletes a way). The report
lists those pairs under "Pieces that could be combined"; if nothing else needs
the cut, select both in JOSM and use Tools -> Combine Way before uploading.

Left alone on an update: a stretch the new survey rates 0 (not assessed) or
marks as paved keeps its tags, since 0 means "not rated" rather than "no
rating", and shows under "Not applied"; and nodes from an earlier run whose
points the new survey no longer exports, which you'll need to clear by hand.

## OSM policies

OSM's [Automated edits](https://wiki.openstreetmap.org/wiki/Automated_edits)
page and the [Automated Edits code of
conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
cover changes made "without review individually". This tool is built to
stay on the right side of that line: it uploads nothing, and the review
steps above have you look at each split and each node in JOSM before you
upload. That holds only if you do the review. A batch of forty trails can
produce a hundred new ways, and if you'd rather skim than check each one,
treat the run as a mechanical edit and follow the code of conduct
(a documentation page under "Automated edits/username", discussion first,
and `mechanical=yes` on the changeset). When a run changes or creates more
than 100 OSM elements, the tool says so, on the terminal and at the top of
the report, and suggests smaller batches: one trail system or one survey
session per run keeps the review manageable.

The parts of the code of conduct that apply either way are already in the
workflow: edits are based on current OSM data (`--refresh` when in doubt),
one changeset covers one area, the changeset comment is plain language, and
the source tag names the ITRS version, the survey dates, and the tool
version, so the edits are identifiable and revertable.

ITRS ratings are a third party's survey data, so bringing them into OSM is
also an import in the wiki's sense. Before the first upload in an area,
check the [Import
guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines): that
the rater and ITRS agree to the data going into OSM under the ODbL, and
that the local community has been told.

## What it won't do

It won't upload anything, by design. It doesn't edit access or oneway tags
either; where the survey disagrees with OSM about direction or who may use
the trail, the report says so under "Advisories" and leaves the decision to
you.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check itrs_to_osm tests
```

The tests run offline on synthesized trails and OSM networks
(`tests/fixtures/builder.py` has twelve scenarios with ground truth, each
modeled on a real case, and `tests/test_resurvey.py` adds overlapping dated
tracks). Real ITRS exports are private and never committed. If some are
present in `research/exports/`, `tests/test_private_samples.py` runs on them
too. `research/README.md` describes that directory.

## Thanks

Mischa Crumbach and ITRS (https://itrs.bike/) for the rating system, the
sample exports, and the data documentation. Nick Ryan of the [Copper Harbor
Trails Club](https://www.copperharbortrails.org/) for the ITRS GeoJSON
exports from Copper Harbor that were used to test and validate this tool.
The OSM wiki pages on the JOSM file format and the Overpass API, and JOSM's
own `SplitWayCommand`, for the rules this tool copies.

This tool was developed with Claude Code (AI-assisted); the design, review,
and decisions are mine.
