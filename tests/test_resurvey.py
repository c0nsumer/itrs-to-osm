"""Re-surveys: a newer export that overlaps an older one, and re-exports.

Copper Harbor's raters update a corner by recording a short new track over
the original (ITRS can't edit a segment in place), so the batch holds both.
The newer survey must win the overlap, its points must replace the older
ones, and an identical re-export must be called out.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fixtures.builder import (
    Network,
    _seg,
    exposure_point,
    itrs_export_along,
    legs,
    point_along,
    points_along,
)

from itrs_to_osm.cli import EXIT_OK, main
from itrs_to_osm.itrs import flag_reexports, is_reexport, parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import Way, read_osm_xml
from itrs_to_osm.points import PointOptions, apply_points
from itrs_to_osm.report import RunContext, write_markdown
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY, existing_itrs

PATH_TAGS = {"highway": "path", "bicycle": "designated"}
OLD, NEW = "2026-06-01T10:00:00Z", "2026-09-25T10:00:00Z"


def dated(export: dict, when: str) -> dict:
    for f in export["features"]:
        if f["geometry"]["type"] == "LineString":
            f["properties"]["segment_last_mutated_on_utc"] = when
    return export


def scene(*, original_points=(), update_points=(), update_rating=(4, 1)):
    """One 600 m way; 'Original' rates 200/200/200 m (2,1)/(2,1)/(3,1) in
    June, 'Update' re-rates 250-350 m as ``update_rating`` in September."""
    net = Network()
    path = legs((0, 0), [(250, 60), (350, 95)])
    nodes = points_along(path, [float(d) for d in range(0, 601, 35)] + [600.0])
    way = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail"})
    original = itrs_export_along(
        net,
        path,
        [_seg(200, 2, 1), _seg(200, 2, 1), _seg(200, 3, 1)],
        track_name="Original",
        points=list(original_points),
    )
    sub = [point_along(path, float(d)) for d in range(250, 351, 10)]
    update = itrs_export_along(
        net,
        sub,
        [_seg(100, *update_rating)],
        track_name="Update",
        points=list(update_points),
    )
    return net.to_xml(), dated(original, OLD), dated(update, NEW), way


def run(xml, exports, *, force=False):
    data = read_osm_xml(xml)
    tracks = [
        parse_track(e, source=e["features"][0]["properties"]["point_start_track_name"])
        for e in exports
    ]
    matches = [match_track(t, data, MatchOptions()) for t in tracks]
    result = apply_matches(data, matches, CutOptions(force=force))
    points = apply_points(data, matches, result, PointOptions(force=force))
    return data, tracks, matches, result, points


def pieces_of(data, result, way_id):
    change = next(w for w in result.ways if w.way_id == way_id)
    return [(round(p.start_m), round(p.end_m), existing_itrs(p.tags_after)) for p in change.pieces]


def test_newer_survey_wins_even_when_shorter():
    xml, original, update, way = scene()
    data, _, _, result, _ = run(xml, [original, update])
    got = pieces_of(data, result, way)
    assert [g[2] for g in got] == [
        {TECH_KEY: "2", EXPOSURE_KEY: "1"},
        {TECH_KEY: "4", EXPOSURE_KEY: "1"},
        {TECH_KEY: "2", EXPOSURE_KEY: "1"},
        {TECH_KEY: "3", EXPOSURE_KEY: "1"},
    ]
    assert abs(got[1][0] - 250) <= 6 and abs(got[1][1] - 350) <= 6
    change = result.ways[0]
    assert any("the newer survey (2026-09-25) wins" in n for n in change.notes)
    (overlap,) = change.overlaps
    assert overlap.rule == "newer"
    assert overlap.winner == ["Update / S001"]
    assert overlap.winner_surveyed == "2026-09-25" and overlap.loser_surveyed == "2026-06-01"
    assert overlap.loser_tags == {TECH_KEY: "2", EXPOSURE_KEY: "1"}
    assert round(overlap.length) in range(95, 106)


def test_order_of_files_does_not_matter():
    xml, original, update, way = scene()
    a = pieces_of(*run(xml, [original, update])[0::3], way)
    b = pieces_of(*run(xml, [update, original])[0::3], way)
    assert a == b


def test_same_date_falls_back_to_the_longer_claim():
    xml, original, update, way = scene()
    dated(update, OLD)
    data, _, _, result, _ = run(xml, [original, update])
    assert [g[2] for g in pieces_of(data, result, way)] == [
        {TECH_KEY: "2", EXPOSURE_KEY: "1"},
        {TECH_KEY: "3", EXPOSURE_KEY: "1"},
    ]
    (overlap,) = result.ways[0].overlaps
    assert overlap.rule == "longer"
    assert any("the longer claim wins" in n for n in result.ways[0].notes)


def test_older_point_on_a_re_rated_stretch_is_stale():
    xml, original, update, way = scene(
        original_points=[exposure_point(300, 2), exposure_point(500, 2)],
        update_points=[exposure_point(300, 2)],
    )
    data, _, _, result, points = run(xml, [original, update])
    by = {(c.track_name, round(c.point.props.get("at", 0))): c for c in points.changes}
    statuses = [(c.track_name, c.status) for c in points.changes]
    assert statuses == [("Original", "skipped"), ("Original", "tagged"), ("Update", "tagged")]
    stale = points.changes[0]
    assert (
        "stale" in stale.reason and "2026-09-25" in stale.reason and "Update / S001" in stale.reason
    )
    assert stale.node_id is None
    # The update's own point got the node on the re-rated piece.
    fresh = points.changes[2]
    piece = next(p for p in result.ways[0].pieces if p.way_id == fresh.way_id)
    assert piece.tags_after[TECH_KEY] == "4" and piece.surveyed == "2026-09-25"
    assert data.nodes[fresh.node_id].tags[EXPOSURE_KEY] == "2"
    assert by  # keeps the helper honest if props change


def test_vertex_just_outside_the_interval_is_clamped_in():
    """A GPS backtrack can place a point's vertex a few meters before the
    segment's interval starts; within the snap distance it still gets a node."""
    xml, original, _update, way = scene(original_points=[exposure_point(200, 2)])
    data, tracks, matches, result, points = run(xml, [original])
    (change,) = points.changes
    assert change.status == "tagged", change.reason


def test_re_export_is_flagged_not_dropped():
    xml, original, _update, way = scene()
    again = copy.deepcopy(original)
    for f in again["features"]:
        p = f["properties"]
        for key in list(p):
            if key.endswith("_uuid"):
                p[key] = p[key] + "-again"
    track_a = parse_track(original, source="a")
    track_b = parse_track(again, source="b")
    assert is_reexport(track_a, track_b)
    flag_reexports([track_a, track_b])
    assert not track_a.warnings
    assert track_b.warnings and "re-export of 'Original'" in track_b.warnings[0]
    # A changed rating is a different survey, not a re-export.
    changed = copy.deepcopy(again)
    next(f for f in changed["features"] if f["geometry"]["type"] == "LineString")["properties"][
        "segment_tech_rating"
    ] = 5
    assert not is_reexport(track_a, parse_track(changed, source="c"))
    # Both are still processed; the claims coincide, so nothing doubles up.
    data, _, _, result, _ = run(xml, [original, again])
    assert [g[2] for g in pieces_of(data, result, way)] == [
        {TECH_KEY: "2", EXPOSURE_KEY: "1"},
        {TECH_KEY: "3", EXPOSURE_KEY: "1"},
    ]
    assert not result.ways[0].overlaps


def test_names_are_trimmed():
    xml, original, _update, _way = scene()
    for f in original["features"]:
        p = f["properties"]
        for key in ("point_start_track_name", "segment_track_name", "segment_name"):
            if key in p:
                p[key] = p[key] + " "
    track = parse_track(original, source="x")
    assert track.name == "Original"
    assert all(s.name == s.name.strip() and s.track_name == "Original" for s in track.segments)


def report_for(xml, exports, tmp_path, *, force=False):
    data, tracks, matches, result, points = run(xml, exports, force=force)
    ctx = RunContext(
        tracks=tracks,
        matches=matches,
        result=result,
        points=points,
        data=data,
        match_options=MatchOptions(),
        cut_options=CutOptions(force=force),
        point_options=PointOptions(force=force),
        osm_source="test.osm",
        osm_base="2026-10-11T00:00:00Z",
        generated_at="2026-10-11T12:00:00+00:00",
        tool_version="0.0.test",
        osm_path=Path("out.osm"),
    )
    path = tmp_path / "report.md"
    write_markdown(ctx, path)
    return path.read_text(encoding="utf-8")


def test_report_lists_overlapping_surveys(tmp_path):
    xml, original, update, _way = scene()
    md = report_for(xml, [original, update], tmp_path)
    assert "- Overlapping surveys: 1 (see Advisories)" in md
    section = md.split("### Overlapping surveys")[1].split("###")[0]
    assert "Update / S001 (technical_difficulty 4, exposure 1, surveyed 2026-09-25)" in section
    assert (
        "over Original / S001, S002 (technical_difficulty 2, exposure 1, "
        "surveyed 2026-06-01)" in section
    )
    assert "the newer survey wins" in section
    assert "- ITRS export format: " in md


def test_report_points_out_pieces_that_could_be_combined(tmp_path):
    """Two ways from an earlier split, both re-rated alike by the new survey."""
    net = Network()
    path = legs((0, 0), [(150, 80), (150, 100), (100, 70)])
    way = net.polyline(path, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=20)
    export = dated(
        itrs_export_along(net, path, [_seg(400, 2, 1)], track_name="Original", offset_m=1.0),
        NEW,
    )
    xml = net.to_xml()
    data = read_osm_xml(xml)
    trail = data.ways[way]
    nodes = list(trail.nodes)
    k = len(nodes) // 2
    # Split the way as an earlier run would have, with the old ratings.
    second = Way(id=9999, nodes=nodes[k:], tags=dict(trail.tags))
    data.set_way_nodes(trail, nodes[: k + 1])
    data.add(second)
    for w in (trail, second):
        w.tags[TECH_KEY] = "3"
        w.tags[EXPOSURE_KEY] = "1"
    track = parse_track(export, source="x")
    matches = [match_track(track, data, MatchOptions())]
    result = apply_matches(data, matches, CutOptions(force=True))
    points = apply_points(data, matches, result, PointOptions(force=True))
    assert {w.way_id for w in result.modified_ways} == {way, 9999}
    ctx = RunContext(
        tracks=[track],
        matches=matches,
        result=result,
        points=points,
        data=data,
        match_options=MatchOptions(),
        cut_options=CutOptions(force=True),
        point_options=PointOptions(force=True),
        osm_source="test.osm",
        osm_base="2026-10-11T00:00:00Z",
        generated_at="2026-10-11T12:00:00+00:00",
        tool_version="0.0.test",
        osm_path=Path("out.osm"),
    )
    path = tmp_path / "report.md"
    write_markdown(ctx, path)
    md = path.read_text(encoding="utf-8")
    section = md.split("### Pieces that could be combined")[1].split("###")[0]
    assert f"w{way} and w9999" in section or f"w9999 and w{way}" in section
    assert f"meet at n{nodes[k]}" in section
    assert "Tools -> Combine Way" in section


def test_cli_warns_about_a_re_export(tmp_path, capsys):
    xml, original, _update, _way = scene()
    again = copy.deepcopy(original)
    for f in again["features"]:
        p = f["properties"]
        for key in list(p):
            if key.endswith("_uuid"):
                p[key] = p[key] + "-again"
    osm = tmp_path / "area.osm"
    osm.write_text(xml, encoding="utf-8")
    a = tmp_path / "geojson-export_Original.geojson"
    b = tmp_path / "geojson-export_Original Sync 2.geojson"
    a.write_text(json.dumps(original), encoding="utf-8")
    b.write_text(json.dumps(again), encoding="utf-8")
    rc = main([str(a), str(b), "--osm", str(osm), "--out", "batch", "--dry-run"])
    assert rc == EXIT_OK
    md = (tmp_path / "batch.report.md").read_text(encoding="utf-8")
    assert "**Warnings:**" in md
    assert "appears to be a re-export of 'Original'" in md
