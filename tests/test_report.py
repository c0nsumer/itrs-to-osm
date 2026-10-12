"""The Markdown and HTML reports over the synthetic scenarios."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from fixtures.builder import SCENARIOS

from itrs_to_osm.html_report import _script_json, write_html
from itrs_to_osm.itrs import parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import read_osm_xml
from itrs_to_osm.report import RunContext, changeset_suggestion, write_markdown
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY

SECTIONS = [
    "# itrs-to-osm report",
    "## Summary",
    "## ITRS track: Test Trail",
    "## OSM ways changed",
    "## Not applied",
    "## Advisories",
    "## How to review in JOSM",
    "## Changeset tags",
]


def run_ctx(name, *, force=False, edit_osm=None, edit_export=None, dry_run=False):
    """Run the pipeline on a scenario (like tests/test_split.py) and wrap it."""
    xml, export, expect = SCENARIOS[name]()
    if edit_export:
        edit_export(export)
    track = parse_track(export, source=name)
    data = read_osm_xml(xml)
    if edit_osm:
        edit_osm(data, expect)
    match_options, cut_options = MatchOptions(), CutOptions(force=force)
    matches = [match_track(track, data, match_options)]
    result = apply_matches(data, matches, cut_options)
    ctx = RunContext(
        tracks=[track],
        matches=matches,
        result=result,
        data=data,
        match_options=match_options,
        cut_options=cut_options,
        osm_source="test.osm",
        osm_base="2026-10-11T00:00:00Z",
        generated_at="2026-10-11T12:00:00+00:00",
        tool_version="0.0.test",
        osm_path=None if dry_run else Path("Test Trail.osm"),
    )
    return ctx, expect


def markdown(ctx, tmp_path) -> str:
    path = tmp_path / "report.md"
    write_markdown(ctx, path)
    return path.read_text(encoding="utf-8")


def segment_lines(export):
    return [f for f in export["features"] if f["geometry"]["type"] == "LineString"]


def test_sections_in_order_and_header(tmp_path):
    ctx, expect = run_ctx("single_way_three_segments")
    md = markdown(ctx, tmp_path)
    positions = [md.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    assert "as of 2026-10-11T00:00:00Z" in md
    assert "`Test Trail.osm`" in md
    assert "corridor 25 m" in md and "min piece 15 m" in md and "force off" in md
    assert "3 matched, 0 partial, 0 unmatched, 0 not applied" in md


def test_split_way_details(tmp_path):
    ctx, expect = run_ctx("single_way_three_segments")
    md = markdown(ctx, tmp_path)
    way = expect["ways"]["trail"]
    assert f"### w{way} Test Trail (path, 600 m): split into 2 pieces" in md
    assert f"https://www.openstreetmap.org/way/{way}" in md
    assert re.search(r"cut at 40\d m: new node inserted \(id -\d+\)", md)
    assert f"w{way} (keeps id)" in md and "new way -" in md
    assert f"{TECH_KEY}=**3**" in md and f"{EXPOSURE_KEY}=**1**" in md
    assert "| 2 (Intermediate) | 1 (Normal injury risk) |" in md
    assert f"| w{way} |" in md  # the segment table's OSM way column
    assert "OSM ways modified: 1 (1 split)" in md


def test_changeset_block(tmp_path):
    ctx, _ = run_ctx("single_way_uniform")
    md = markdown(ctx, tmp_path)
    comment, source = changeset_suggestion(ctx)
    assert f"```\ncomment={comment}\nsource={source}\n```" in md
    assert "upload dialog" in md and "provenance" in md


def test_refusal_reported_prominently(tmp_path):
    ctx, expect = run_ctx("existing_itrs_tags")
    md = markdown(ctx, tmp_path)
    way = expect["ways"]["trail"]
    assert "Nothing was written for those ways" in md and "--force" in md
    assert f"### w{way} Test Trail (path, 300 m): refused" in md
    assert (
        f"{TECH_KEY} is 2 on this way, the survey says 3 (from Test Trail / S001); not changed"
    ) in md
    assert "Refusals: 1" in md


def test_forced_overwrite_is_an_advisory(tmp_path):
    ctx, expect = run_ctx("existing_itrs_tags", force=True)
    md = markdown(ctx, tmp_path)
    way = expect["ways"]["trail"]
    assert "Nothing was written" not in md
    advisories = md[md.index("## Advisories") :]
    assert f"w{way} (Test Trail): {TECH_KEY} 2 -> 3 (from Test Trail / S001)" in advisories
    assert "force on" in md


def test_paved_road_not_applied(tmp_path):
    ctx, expect = run_ctx("paved_road_segment")
    md = markdown(ctx, tmp_path)
    road = expect["ways"]["road"]
    assert "0 unmatched, 1 not applied" in md
    assert "matched, not applied" in md
    not_applied = md[md.index("## Not applied") : md.index("## Advisories")]
    assert f"Test Trail / S002 on w{road}: paved road" in not_applied
    assert f"### w{road}" not in md  # the road is never edited


def test_not_rated_key_listed(tmp_path):
    ctx, _ = run_ctx("not_rated_zero")
    md = markdown(ctx, tmp_path)
    not_applied = md[md.index("## Not applied") : md.index("## Advisories")]
    assert f"Test Trail / S001: `{EXPOSURE_KEY}` not rated (0)" in not_applied
    assert "| 3 (Advanced) | not rated |" in md


def test_unmatched_segment_and_dropped_tail(tmp_path):
    ctx, _ = run_ctx("segment_far_from_any_way")
    md = markdown(ctx, tmp_path)
    assert "1 unmatched" in md
    assert "Test Trail / S002: unmatched (" in md

    ctx, expect = run_ctx("tail_onto_other_way")
    md = markdown(ctx, tmp_path)
    other = expect["ways"]["other"]
    assert re.search(
        rf"Test Trail / S002: the last 1\d m of this \d+ m segment ran onto w{other}", md
    )
    assert "stays untagged because it is shorter than the 15 m minimum piece" in md


def test_relation_line_and_name_match(tmp_path):
    ctx, expect = run_ctx("way_in_route_relation")
    md = markdown(ctx, tmp_path)
    rel = expect["relations"]["route"]
    assert f"relation {rel} Test Trail (route): pieces inserted forward" in md
    assert "OSM relations updated: 1" in md
    assert f"by relation {rel} name 'Test Trail'" in md


def test_name_match_absent(tmp_path):
    def rename(data, expect):
        data.ways[expect["ways"]["trail"]].tags["name"] = "Something Else Entirely"

    ctx, _ = run_ctx("single_way_uniform", edit_osm=rename)
    md = markdown(ctx, tmp_path)
    assert "no name match; matched by geometry alone" in md


def test_empty_sections_say_none_or_nothing(tmp_path):
    def rate_exposure(export):
        for f in segment_lines(export):
            f["properties"]["segment_max_exposure"] = 1

    ctx, _ = run_ctx("single_way_uniform", edit_export=rate_exposure)
    md = markdown(ctx, tmp_path)
    assert "## Not applied\n\nITRS data that produced no OSM edit, and why.\n\nNothing." in md
    assert "### Existing ITRS values overwritten\n\nNone." in md
    assert "### Direction\n\nNone." in md
    assert "### ITRS rating points\n\nNone." in md


def test_direction_and_use_advisories(tmp_path):
    def edit_osm(data, expect):
        tags = data.ways[expect["ways"]["trail"]].tags
        tags["oneway"] = "yes"
        tags["foot"] = "no"
        tags["bicycle"] = "no"

    ctx, expect = run_ctx("single_way_uniform", edit_osm=edit_osm)
    md = markdown(ctx, tmp_path)
    way = expect["ways"]["trail"]
    assert f"w{way} Test Trail: ITRS says Bi-directional, OSM has oneway=yes (S001)" in md
    assert f"w{way} Test Trail: ITRS says Shared use, OSM has foot=no (S001)" in md
    assert f"w{way} Test Trail: OSM has bicycle=no on a rated trail (S001)" in md


def test_one_way_bike_only_against_two_way_path(tmp_path):
    def edit_export(export):
        for f in segment_lines(export):
            f["properties"]["segment_use"] = "Bike-only"
            f["properties"]["segment_direction"] = "Uni-directional"

    ctx, expect = run_ctx("single_way_uniform", edit_export=edit_export)
    md = markdown(ctx, tmp_path)
    way = expect["ways"]["trail"]
    assert f"w{way} Test Trail: ITRS says Uni-directional, OSM has no oneway tag (two-way)" in md
    assert (
        f"w{way} Test Trail: ITRS says Bike-only, OSM has no foot tag, and highway=path "
        "allows walking by default"
    ) in md


def add_points(export):
    first = segment_lines(export)[0]
    props = first["properties"]
    coords = first["geometry"]["coordinates"][3]
    common = {
        "point_segment_uuid": props["segment_uuid"],
        "point_segment_name": props["segment_name"],
        "point_track_name": "Test Trail",
    }
    export["features"].append(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "point_type": "Exposure",
                "point_exposure_rating": 3,
                "point_exposure_width_of_trail_bed_cm": "65",
                "point_remark": "drop </script> & <b>cliff</b>",
                "point_photo_1": "https://example.com/p.jpg?a=1&b=2",
                **common,
            },
        }
    )
    export["features"].append(
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "point_type": "RiverCrossing",
                "point_riverCrossing_value": "Blue",
                **common,
            },
        }
    )


def test_rating_points_listed(tmp_path):
    ctx, _ = run_ctx("single_way_uniform", edit_export=add_points)
    md = markdown(ctx, tmp_path)
    points = md[md.index("### ITRS rating points") : md.index("## How to review")]
    assert "| S001 | Exposure | 3 (Life threatening consequences) |" in points
    assert "width of trail bed cm: 65" in points
    assert "[photo 1](https://example.com/p.jpg?a=1&b=2)" in points
    assert "\\<b>cliff\\</b>" in points  # escaped for Markdown
    assert "| S001 | RiverCrossing | Blue |" in points
    assert "https://www.openstreetmap.org/?mlat=" in points


def test_dry_run_review_steps(tmp_path):
    ctx, _ = run_ctx("single_way_uniform", dry_run=True)
    md = markdown(ctx, tmp_path)
    assert "- Output: dry run, no .osm written" in md
    steps = md[md.index("## How to review in JOSM") :]
    assert "1. This was a dry run" in steps
    assert "`modified`" in steps and "only the modified and new elements" in steps


# --- HTML ----------------------------------------------------------------------


class _Collector(HTMLParser):
    VOID = {"meta", "link", "br", "img", "input", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.scripts: list[str] = []
        self.links: list[dict] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            self.links.append(dict(attrs))
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with open {self.stack[-3:]}")
            return
        self.stack.pop()
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data


def html_report(ctx, tmp_path) -> str:
    path = tmp_path / "report.html"
    write_html(ctx, path)
    return path.read_text(encoding="utf-8")


def map_json(doc: str) -> dict:
    start = doc.index("const MAP_DATA = ") + len("const MAP_DATA = ")
    end = doc.index(";\nconst LEGEND_HTML")
    return json.loads(doc[start:end])


def test_html_parses_and_has_map(tmp_path):
    ctx, expect = run_ctx("way_in_route_relation", edit_export=add_points)
    doc = html_report(ctx, tmp_path)
    parser = _Collector()
    parser.feed(doc)
    parser.close()
    assert not parser.errors and not parser.stack
    assert 'src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"' in doc
    assert any(
        link.get("href", "").endswith("leaflet@1.9.4/dist/leaflet.css") for link in parser.links
    )
    assert "OpenStreetMap</a> contributors" in doc
    # Two scripts: Leaflet and the inline map. The remark's "</script>" stays inside.
    assert doc.count("</script>") == 2
    assert "<\\/b>" in doc  # every "</" in the embedded JSON is escaped
    assert _script_json({"x": "</script>"}) == '{"x": "<\\/script>"}'
    data = map_json(doc)
    assert data["bounds"] and len(data["points"]) == 2
    colors = {line["style"]["color"] for line in data["lines"]}
    assert "#0080c6" in colors and "#ee3124" in colors  # tech 2 and 3 pieces
    for section in (
        "Summary",
        "ITRS track: Test Trail",
        "OSM ways changed",
        "Not applied",
        "Advisories",
    ):
        assert f">{section}</h2>" in doc
    assert "<table>" in doc
    rel = expect["relations"]["route"]
    assert f"relation {rel} Test Trail (route): pieces inserted forward" in doc


def test_html_escapes_tag_values(tmp_path):
    def edit_osm(data, expect):
        data.ways[expect["ways"]["trail"]].tags["name"] = "Test Trail <b> & Co"

    ctx, expect = run_ctx("single_way_uniform", edit_osm=edit_osm, edit_export=add_points)
    doc = html_report(ctx, tmp_path)
    assert "Test Trail <b> & Co" not in doc
    assert "Test Trail &lt;b&gt; &amp; Co" in doc
    assert "<b>cliff</b>" not in doc
    data = map_json(doc)
    popups = " ".join(line["popup"] for line in data["lines"])
    assert "Test Trail &lt;b&gt; &amp; Co" in popups
    point_popups = " ".join(p["popup"] for p in data["points"])
    assert "&lt;/script&gt; &amp; &lt;b&gt;cliff&lt;/b&gt;" in point_popups
    assert 'href="https://example.com/p.jpg?a=1&amp;b=2"' in point_popups


def test_html_unmatched_segment_in_grey(tmp_path):
    ctx, _ = run_ctx("segment_far_from_any_way", dry_run=True)
    data = map_json(html_report(ctx, tmp_path))
    assert any(line["style"]["color"] == "#595959" for line in data["lines"])
