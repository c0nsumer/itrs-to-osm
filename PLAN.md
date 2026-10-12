# itrs-to-osm: plan

Drafted 2026-Oct-11 with Claude Code from the project brief, the four sample
ITRS exports, and live OSM data for those areas. Nothing here is built yet.
This is the proposal to review before development starts.

## The problem

Certified ITRS trail raters walk or ride a trail with the ITRS app and get a
GeoJSON export: one LineString per rated segment, each carrying a technical
difficulty and an exposure rating. Those trails are already in OSM, but the
surveyed track never lines up exactly with the OSM way, and one OSM way
usually spans many ITRS segments with different ratings.

Copying the ratings over by hand means finding the way, working out where
each segment boundary falls, splitting the way there, and typing the tags.
That's tedious and error-prone. itrs-to-osm does the mechanical part and hands
the result to a human in JOSM for review and upload. It never talks to the OSM
API itself.

## What the samples showed

All four exports share the same shape. Details that matter for the design:

- Segments are contiguous (each starts exactly where the previous one ends),
  ordered by `segment_position_in_trail`, with ~8 m between vertices. The
  geometry length matches `segment_length_m` to within a meter.
- The Start point sits on the first vertex of S001. Exposure and
  GeneralAssessmentRemark points sit exactly on a segment vertex and
  reference their segment by `point_segment_uuid`.
- ITRS 4.4 (Visp) uses `segment_trail_name` / `segment_trail_uuid` and has no
  top-level `version` / `date`. ITRS 5.2 and 5.3 use `segment_track_name` /
  `segment_track_uuid`. Point features use `point_track_*` in both. Some
  numeric fields arrive as strings in one version and numbers in another
  (eg: `segment_max_offcamber_deg` is `"0"` in one segment and `14.0` in the
  next).
- Technical difficulty values seen: 1 to 4. Exposure values seen: 0 to 3.
  The Start point also reports `techLevel_0` share percentages.

The live OSM data (fetched 2026-Oct-11 via Overpass) lines up well:

| Track | OSM ways touched | Median offset | 90th pct | Max |
|---|---|---|---|---|
| KPT Fish Cove Pt. 1 (23 segments) | 1 cycleway, 8.8 km, 533 nodes | 1 to 3 m | 3 to 7 m | 9 m |
| Loam Rider (6 segments) | 5 cycleways at a junction cluster | 0.2 to 8 m | 1 to 13 m | 18 m |
| On the Edge (4 segments) | 1 cycleway, 527 m | 2 to 5 m | 3 to 7 m | 7 m |
| Visp Phoenix (13 segments) | 1 path, 3.0 km, 571 nodes | 1 to 3 m | 2 to 12 m | 21 m |

Things that will bite a naive "nearest way" approach:

- Junctions and parallel trails. Loam Rider starts at a four-way junction and
  its first 8 vertices flip between Thunderbolt and Loam Rider. Fish Cove
  brushes a parallel `highway=track` twice. Visp S011 flickers across three
  ways at a crossing. Matching needs continuity, not per-point nearest.
- Tails. Loam Rider's last 5 vertices run onto Flo'Rion. Visp's last 3
  vertices run onto a `highway=track`. On the Edge ends one vertex into
  Woopidy Woo. These are real but tiny overlaps that shouldn't produce 10 m
  slivers of split way.
- Segment boundaries almost never land on an existing OSM node. The nearest
  node is typically 1 to 15 m away. Splitting means inserting new nodes on the
  OSM way geometry.
- Rating changes don't happen at landmarks. Track starts and ends do sit at
  junctions (1 to 15 m away in all four samples, since raters start at
  trailheads and junctions). But of the 31 rating changes inside the
  tracks, only 3 fall within 15 m of a junction or way end. The rest are 17 m
  to 1 km from the nearest one, out where the trail character changes
  (a rock garden starts, the grade eases). So junctions can anchor the
  track ends and the occasional interior cut, but most cuts have to be
  placed from the GPS track.
- Existing tags. Every matched way already has `mtb:scale:imba`, and the
  Visp way also has `mtb:scale`. These must be left alone. No way anywhere in
  OSM has a `mtb:scale:itrs:*` tag yet (taginfo, 2026-Oct-11), so this tool
  will create the first ones.
- Adjacent segments often share a rating. Merging them before cutting
  reduces Fish Cove from 22 internal cuts to 17, Visp from 12 to 10, Loam
  Rider from 5 to 3, On the Edge from 3 to 1.

The ITRS document "ITRS rating data explanation" (Crumbach and Melchiori,
February 2026, kept locally and out of the repo) answers what 0 means. Page 9
translates every rating value (technical difficulty, exposure, river
crossing) as: 0 = Not rated, 1 = Green, 2 = Blue, 3 = Red, 4 = Black,
5 = Orange. So Loam Rider's `segment_max_exposure: 0` on every segment means
exposure was never assessed there, which also explains why its track-level
max exposure reads 2 while the segments read 0. A "not rated" value must not
become an OSM tag.

Other facts from that document worth designing around:

- The top-level `version: "1.0"` is the GeoJSON file format version, separate
  from `segment_itrs_version` (the rating criteria version). The Visp export
  predates the file version key.
- A segment's exposure is the rating of its most exposed section. Exposure
  points are only exported for blue or worse, with length and photos for red
  or worse. Exposed sections are typically 5 to 30 m long, while segments
  are typically 100 to 500 m.
- Altitude in the LineStrings is barometric.
- ITRS publishes official colors per level and asks that displays use them.
  The HTML report map will.

The ITRS QGIS assets (palette, QML layer styles, icons, and two QGIS helper
scripts; also kept locally and out of the repo) fill in the details:

- Palette: 1 green `#72bf44`, 2 blue `#0080c6`, 3 red `#ee3124`, 4 black
  `#000000`, 5 orange `#f68b1f`. Roads are purple `#844c82`, grey `#595959`
  is available for unmatched, and yellow `#ffeb33` marks bike-only segments.
- The line style confirms the 0 handling: paved roads draw purple whatever
  their rating, an unsurfaced road rated 0 draws purple, and there is no
  "0 - Single trail" rule at all. So 0 is "road, not rated".
- `segment_track_type` has three values: "Paved/surfaced road", "Unsurfaced
  road/double track", "Single trail". Matching uses this as a hint.
- Point types beyond the ones in the samples: Water, RockfallZone,
  RiverCrossing (four text categories, green to black), Wildlife. Each
  carries its value in `point_<type>_value`. The parser should accept any
  `point_type`, keep its fields, and list them in the report rather than
  failing on an unknown one.
- The QGIS scripts only style and arrange layers; they contain no parsing
  logic to borrow.

## The tool

A command line program, run from the directory holding the export:

```
itrs-to-osm "geojson-export_On the Edge.geojson"
```

That produces, next to the input:

- `On the Edge.osm`: a JOSM file with the area download plus the proposed
  changes, flagged so JOSM shows them as modified.
- `On the Edge.report.md`: what changed, what didn't, and why.
- `On the Edge.report.html` (stretch goal): the same report with a map.

The user opens the `.osm` in JOSM, reviews (the validator and the `modified`
search filter both help), fixes anything the tool got wrong, writes a
changeset comment, and uploads. The tool is one step in the data preparation
process, not an editor.

Options, all with sensible defaults:

- `--osm FILE`: use a local `.osm` (eg: saved from JOSM) instead of querying
  Overpass. (Needed for offline tests, and handy when Overpass is busy.)
- `--out NAME`: base name for the combined `.osm` and report (default: the
  input name for one file, required for several).
- `--out-dir DIR`: where to write outputs (default: next to the input).
- `--overpass-url URL`: alternate Overpass instance.
- `--refresh`: ignore the cached Overpass response.
- `--corridor M`, `--max-offset M`, `--snap M`, `--snap-junction M`,
  `--min-piece M`: the matching thresholds described below.
- `--name NAME`, `--way-id ID[,ID...]`: pin the matching to a trail name or
  to specific ways when the automatic choice is wrong.
- `--html`: also write the HTML report.
- `--force`: overwrite existing ITRS values that differ (default: refuse
  and report).
- `--dry-run`: match and report, but write no `.osm`.
- `-v` / `--verbose`, `--version`.

## Several exports in one run

The tool takes any number of GeoJSON files and produces one `.osm` and one
report for the batch:

```
itrs-to-osm geojson-export_*.geojson --out copper-harbor-2026-10
```

This isn't only convenience. Two tracks often touch the same OSM way: Loam
Rider's tail runs onto Flo'Rion, and "KPT Fish Cove Pt. 1" implies a Pt. 2
on the same 8.8 km way. If each export produced its own `.osm`, both files
would modify that way from the same base version. Uploading the first makes
the second stale, and JOSM hands the reviewer a conflict to resolve by hand.
One combined file avoids that, and also lets the second track's rating win
over the first track's short tail instead of leaving a stray sliver.

How it works:

- Download per file (bounding box plus buffer), merging boxes that overlap,
  and merge all responses into one in-memory OSM model keyed by element id.
  Files far apart (Copper Harbor and Visp) stay separate downloads but
  share the model.
- Process tracks one after another against that model, so a way split by
  the first track is already split when the second track reaches it.
- Where two tracks claim the same stretch of way with different ratings,
  the longer overlap wins, the tail rule drops the short one, and the
  report lists the conflict under advisories with both values.
- The report gets one section per track plus a batch summary. The `.osm`
  name comes from `--out`, or from the input name when there is one file.

A single file is the same code path with a batch of one.

## Pipeline

1. Parse the ITRS GeoJSON. Resolve field names through one alias table
   (`segment_track_name` or `segment_trail_name`, etc). Validate: one Start
   point, segments contiguous and in order, ratings in range. Coerce
   string-or-number fields. Keep the Exposure and Remark points for the
   report.
2. Find the area. Bounding box of all segment vertices, buffered by 100 m.
3. Download via Overpass. One query: every `highway=*` way in the box, their
   nodes, and the parent relations of those ways, with `out meta` (JOSM needs
   `version` on every existing object it uploads; the API rejects a modify
   without it). Cache the response by query hash so reruns are offline and
   reproducible. Retry with backoff on 429 / 504 / busy-server errors,
   following the trailmaps `overpass.py` conventions (descriptive User-Agent,
   single endpoint, patience).
4. Match segments to ways (next section).
5. Plan the cuts and tags per way (section after).
6. Apply: split ways, insert nodes, set tags, update relations.
7. Write the `.osm` and the reports.

## Matching

The goal is: for every ITRS segment, an ordered list of intervals on OSM ways,
each interval being "way W from distance a to distance b along the way".

- Candidates: every downloaded way with a `highway` tag, except `area=yes`.
  The ITRS `segment_track_type` says what kind of way to expect, and the
  scoring prefers the matching OSM class: "Single trail" prefers path,
  cycleway, footway, and bridleway; "Unsurfaced road/double track" prefers
  track and unclassified; "Paved/surfaced road" prefers residential,
  service, unclassified, and tertiary. A mismatch isn't excluded, only
  penalized, so a singletrack segment hugging a forest road still lands on
  the trail.
- Per vertex: project onto every candidate within the corridor (default
  25 m). Record distance and position along the way.
- Sequence decoding: a small Viterbi over (vertex, candidate way). Emission
  cost grows with the offset distance. Transition cost is near zero for
  staying on the same way, low for moving to a way that shares a node near
  the current position (a real junction), and high for jumping to an
  unconnected way (a parallel trail). Position along a way must advance
  monotonically, which kills the junction flicker seen in the samples.
- Name as a strong prior. Before geometry, compare the ITRS track name with
  the `name` of every candidate way and of every parent `route` relation
  (trail names often live on the relation, not the way). Normalize both
  (casefold, strip punctuation) and score exact match highest, then token
  overlap. Ways that match get a large bonus in the decode, so at a junction
  or alongside a parallel trail the named trail wins. It's a prior, not a
  filter: unnamed connector pieces and differently named tails stay
  possible, because a rater's track can legitimately run onto them.
  In the samples, "Loam Rider" and "On the Edge" match exactly, "KPT Fish
  Cove Pt. 1" matches "Keweenaw Point Trail (Keystone - Fish Cove)" on
  tokens, and "Visp Phoenix aus der Asche CTR" shares only "Phoenix" with
  "The Phoenix". So exact match alone would cover half the samples, and
  token overlap all four, but neither can be required.
- Overrides: `--name "OSM name"` pins the prior to a given trail name, and
  `--way-id` restricts matching to listed ways, for when the rater's name
  and the mapper's name disagree. The report always says which named way or
  relation was treated as the trail and why.
- Quality gates, per segment: 90th percentile offset over `--max-offset`
  (default 15 m) means the segment is not applied and goes in the report as
  unmatched. Any resulting interval shorter than `--min-piece` (default 15 m)
  is dropped as a tail or crossing artifact and reported as such.

This runs in well under a second for these sizes (hundreds of vertices by
tens of ways), so there's no need for a spatial index beyond a bounding box
prefilter.

## Cutting and tagging

Per OSM way, collect the intervals from all segments and sort them. Then:

- Merge adjacent intervals whose target tags are identical. No cut between
  two segments rated the same.
- Where the way continues past the track's start or end, cut there, unless
  the leftover piece would be shorter than `--min-piece`. In that case the
  rating extends to the way end and the report says so.
- Realize each cut as a node, preferring places a mapper would recognize.
  First choice: a junction node or way end within `--snap-junction`
  (default 15 m) along the way. Second: any existing node within `--snap`
  (default 5 m). Otherwise insert a new node at the interpolated point on
  the OSM way's own geometry. Never move an existing node, and never use
  the ITRS coordinate (the OSM geometry stays as the mapper drew it). The
  report records which of the three happened for each cut, so the reviewer
  knows which boundaries are anchored to something real and which are
  GPS-placed.
- Split the way the way JOSM does: the piece with the most nodes keeps the
  original id and history, the other pieces get negative ids and a copy of
  every tag. For each parent relation, insert the new pieces next to the
  original member with the same role, ordered by direction for `route`,
  `multipolygon` and `boundary` relations (the JOSM `SplitWayCommand`
  logic). Other relation types get the insertion plus a warning in the
  report. The modified relation goes into the output file too.
- Set `mtb:scale:itrs:technical_difficulty` and `mtb:scale:itrs:exposure` on
  each piece. No other tag is touched.
- If a piece already carries an ITRS value that differs from the new one,
  the tool refuses: the piece is left untouched (no split, no tag), listed
  in the report under "refused: existing ITRS value differs" with both
  values, and the run exits non-zero so a script notices. `--force`
  overwrites instead and records the old value in the report. A piece whose
  existing value already equals the new one is reported as "already
  tagged" and not modified.
- No traceability tag goes on the objects. Provenance belongs in the
  changeset, so the report ends with a suggested changeset `comment` and
  `source` line (track names, ITRS version, survey date) to paste into
  JOSM's upload dialog.
- Segments with `segment_track_type` of "Paved/surfaced road" are never
  tagged, whatever their rating. ITRS itself doesn't present a rating on
  paved roads. They're still matched, so the report can show where they
  landed, and they're listed under "not applied" with the reason. The
  other two track types ("Unsurfaced road/double track" and "Single
  trail") are tagged normally.
- A value of 0 means "Not rated" (ITRS rating data explanation, p. 9). The
  tool writes no tag for it, leaves any existing ITRS tag for that key
  alone, and lists the segment under "not rated" in the report. A segment
  with technical difficulty rated and exposure 0 still gets its technical
  difficulty tag. Valid written values are 1 to 5 for technical difficulty
  and 1 to 4 for exposure; anything else is an input error and is reported,
  not written.
- Ways the track never reached, and pieces of a split way outside the
  track, get no ITRS tags.

The two-tag mapping, with ITRS field first:

- `segment_tech_rating` -> `mtb:scale:itrs:technical_difficulty`
- `segment_max_exposure` -> `mtb:scale:itrs:exposure`

Endurance and wilderness are not in the exports and are not handled.

Two other ITRS fields are compared with OSM and reported, never acted on:

- Direction: `segment_direction` (Uni-directional or Bi-directional)
  against `oneway`. Eg: ITRS says one-way and OSM has no `oneway=yes`.
- Use: `segment_use` (Shared use or Bike-only) against OSM access tags.
  "Use" is ITRS's word; OSM says who may use a way with `foot`, `bicycle`,
  and `mtb` tags, and `highway=path` implies foot access when no tag says
  otherwise. Eg: ITRS says Bike-only and the OSM way has `foot=yes`, or
  no foot tag at all (the Loam Rider case). Or ITRS says Shared use and
  OSM has `foot=no`. `bicycle=no` on a rated trail is flagged as well.

These go under advisories in the report so the reviewer can decide in JOSM.
The tool only ever writes the two ITRS keys.

## The OSM file

JOSM's `.osm` format, as documented on the wiki:

- Everything that was downloaded is written back, so the reviewer sees the
  surrounding trails for context. Unchanged elements keep their `version`,
  `timestamp`, `changeset`, `uid`, and `user` from `out meta`.
- Modified ways and relations carry `action='modify'`. New nodes and ways
  carry negative ids and no version.
- `<bounds>` records the download box, and `generator` is
  `itrs-to-osm/<version>`.
- No `upload='never'`: the whole point is that the reviewer uploads after
  checking. JOSM only sends the elements marked modified or new.

JOSM reading the file, the validator passing, and a test upload to the OSM
dev server are the acceptance checks for this stage.

## The report

Markdown, always. Sections:

- Summary: track name, ITRS version, survey date, counts of segments
  applied / partially applied / not applied, ways modified, ways split, nodes
  added.
- Per segment: rating, which way pieces it landed on, offset stats, and any
  dropped tails.
- Per way: name, OSM id, link, before and after tags, where it was split
  (with the snapped-or-inserted decision), and any relation updates.
- Not applied: each segment or sub-interval with the reason (too far from
  any way, shorter than min piece, ambiguous at a junction).
- Advisories: existing ITRS values overwritten, direction and use
  mismatches, Exposure and Remark points with their notes and photo links
  (useful when deciding whether the exposure rating looks right).

HTML is the stretch goal: the same content plus a Leaflet map showing the
ITRS track, the matched way pieces colored by rating in the official ITRS
colors, and anything unmatched in grey. CyclOSM raster tiles with attribution are fine for a local
review page at this volume.

## Stack

Python 3.11 or later, matching trailmaps.app-map-generator, so the two
projects share conventions and this one can grow into the web service later
(the matching core becomes a library the service imports). Runtime
dependencies kept to two: `shapely` for projection and linear referencing,
`requests` for Overpass. Standard library `xml.etree` handles the OSM XML in
both directions. Dev dependencies: `pytest`, `ruff` (same rule set as
trailmaps).

Layout:

```
itrs_to_osm/
  cli.py        argument parsing, orchestration, exit codes
  itrs.py       GeoJSON parsing, field aliases, validation
  overpass.py   query, cache, retry
  osm.py        node/way/relation model, XML read and write
  geo.py        local projection, point-to-line projection, along-way distance
  match.py      candidate scoring and Viterbi decode
  split.py      cut planning, node insertion, JOSM-style split, relation fixup
  tagging.py    the two-key mapping, value validation, overwrite bookkeeping
  report.py     Markdown (and later HTML) output
tests/
  fixtures/     synthesized ITRS exports and small hand-built .osm networks
  private/      gitignored; the real exports, used when present
```

Install for users: `pipx install git+https://github.com/c0nsumer/itrs-to-osm`
or `uv tool install ...`, which gives an `itrs-to-osm` command on macOS,
Linux, and Windows wherever Python 3.11+ exists. A later release can add
PyInstaller builds from GitHub Actions for a no-Python-needed binary.

The alternative considered was Go (single static binary, `paulmach/orb` and
`paulmach/osm` cover the geometry and OSM XML). It's a reasonable choice, but
Python keeps the code next to the trailmaps project and the future web
service, and shapely is the more mature geometry library. Decision: Python.

## Testing

The real exports stay out of the repo, so the committed test suite runs on
synthesized data:

- A fixture builder that takes a hand-drawn OSM network (straight path,
  junction, parallel braid, a way in a `route` relation) and generates an
  ITRS-shaped GeoJSON from a chosen path through it, with configurable
  offset noise and vertex spacing.
- Cases: one way, uniform rating, no split; several segments on one way with
  splits; adjacent equal ratings merged; boundary snaps to an existing node;
  boundary inserts a node; track spans two ways; junction with a parallel
  way; tail sliver dropped; segment too far away reported; existing ITRS
  tags overwritten and recorded; relation membership updated with correct
  order; longest piece keeps the id; 4.4 and 5.x field names; 0 values;
  cached Overpass response reused; round-trip of an unchanged `.osm`.
- `tests/private/` runs the four real exports against cached Overpass
  responses when the files exist, and skips otherwise.

Every stage also gets checked by opening the output in JOSM.

## Milestones

1. Scaffold: `pyproject.toml`, package layout, `.gitignore` (done, keeps the
   private files out), updated `CLAUDE.md`, ITRS parser with aliases and
   validation, tests for the parser.
2. Overpass client with cache and retry, OSM XML model, read and write
   round-trip that JOSM opens cleanly.
3. Matching with `--dry-run` reporting intervals and offsets. Validate
   against all four samples and tune the defaults.
4. Cutting, splitting, relation fixup, tagging, `.osm` output. Verify in
   JOSM, including a dev-server upload.
5. Markdown report, CLI polish, README in the project voice.
6. HTML report with map (stretch).
7. Release: squash history, tag v1.0.0, publish as c0nsumer/itrs-to-osm.

## Open questions

Settled on 2026-Oct-12:

1. 0 means "Not rated" (ITRS rating data explanation), so the tool skips
   the tag rather than writing 0. Exposure stays per segment from
   `segment_max_exposure`; the finer-grained exposure points are reported
   but not used for cuts.
2. No traceability tag on objects. Provenance goes in the changeset
   `source` and comment, which the report suggests.
3. Thresholds start at corridor 25 m, max offset 15 m, snap 5 m, snap to
   junction 15 m, min piece 15 m, and get tuned against the four samples in
   milestone 3. All are flags.
4. Differing existing ITRS values are refused unless `--force`.
5. Direction and use mismatches are both reported, never acted on.
6. Python.
7. The Visp sample is in Switzerland (Valais), not Italy. The brief has
   been corrected.
8. Paved-road segments are never tagged. They're matched and reported
   only.

## Out of scope for now

The web service version (upload a GeoJSON, download the `.osm` and report),
the OSM wiki page for `mtb:scale:itrs:*`, and adding ITRS as a difficulty
option in trailmaps.app-map-generator (which reads `mtb:scale:imba` today)
are all later work. The design keeps the matching core importable so the
first two reuse it.

## Citations

- ITRS: https://itrs.bike/overview/, https://itrs.bike/technical-difficulty/,
  https://itrs.bike/exposure/, "ITRS rating data explanation"
  (M. Crumbach, E. Melchiori, February 2026), and the ITRS QGIS assets
  (palette, QML styles, icons, scripts). Both are local copies, not in the
  repo.
- OSM wiki: Key:mtb:scale:imba, JOSM file format, Overpass API and Overpass
  QL (`out meta`, `around`, `<` and `>`), API v0.6 (version required on
  modify), Automated Edits code of conduct
- JOSM `SplitWayCommand.java` for the relation handling rules
- taginfo for `mtb:scale:itrs` (no uses as of 2026-Oct-11)
- `~/projects/trailmaps.app-map-generator/scripts/overpass.py` for the
  Overpass client conventions

## Updates

2026-Oct-12: Milestones 1 to 4 built and committed; the report renderers and
README are in progress. Things that changed from the plan above while
building against the real tracks:

- Intervals carry projection-independent positions (edge index plus fraction
  along the edge) as well as meters, so claims from tracks matched in
  different local frames line up exactly on the same way.
- The junction extension that lets neighboring intervals meet at a shared
  node is bounded to 40 m along the way. Without that, Loam Rider's 5-vertex
  tail onto Flo'Rion stretched to 99 m and escaped the minimum piece rule.
- Tails are dropped for offset as well as length: a secondary interval whose
  90th percentile offset exceeds `--max-offset` goes to the report, not the
  map. That's what catches the Loam Rider tail (22 m, 17.7 m off).
- Same-tag claims from one track merge before overlaps are resolved, so a
  track rating a whole way at one level beats another track's shorter claim.
- A way that is the only way member of a relation is treated as direction
  irrelevant (JOSM's word), not unknown, so the report doesn't warn about it.
- shapely turned out unnecessary; the geometry is a few hundred lines of
  pure Python, and the only runtime dependency is `requests`.
- An independent review (Opus) of the matcher and splitter found six bugs
  before any JOSM run, five reproduced: an interval ending at a way's last
  node lost the last edge; a track crossing a loop's closing node tagged
  the far side of the loop; a cut could snap onto a way's end node and
  make a one-node way; relation direction was read from the already
  truncated keeper piece; two cuts could snap to one junction node and
  silently erase the zone between them; and a cut on a node a way visits
  twice used the first occurrence. All fixed with regression tests
  (`tests/test_review_regressions.py`). Along the way, vertices are now
  re-projected for continuity after the decode, which also settles hairpins.
- Pending human check: open the four generated `.osm` files in JOSM.
- 2026-Oct-12: the HTML report's map showed the OSMF "Blocked" tile. The
  standard layer requires a Referer header and a page opened from file://
  never sends one, so the map now uses CyclOSM tiles (OSM France), which
  serve without a Referer and render mtb:scale. The Advisories preamble
  about what the tool doesn't edit was removed at Steve's request: the
  report says what was done, not what wasn't.
