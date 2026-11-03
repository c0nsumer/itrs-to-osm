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
    assert overlap.winner_surveyed == NEW and overlap.loser_surveyed == OLD
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
    assert piece.tags_after[TECH_KEY] == "4" and piece.surveyed == NEW
    assert data.nodes[fresh.node_id].tags[EXPOSURE_KEY] == "2"
    assert by  # keeps the helper honest if props change


def test_newer_survey_downrates_a_point():
    """The rater re-surveys a corner and lowers its exposure from 3 to 2: the
    old point must not keep the node, whatever the ratings say."""
    xml, original, update, _way = scene(
        original_points=[exposure_point(300, 3)],
        update_points=[exposure_point(300, 2)],
        update_rating=(2, 2),
    )
    data, _, _, _result, points = run(xml, [original, update])
    old, new = points.changes
    assert old.status == "skipped" and old.reason.startswith("stale:")
    assert new.status == "tagged"
    assert data.nodes[new.node_id].tags[EXPOSURE_KEY] == "2"
    assert not any(n.tags.get(EXPOSURE_KEY) == "3" for n in data.nodes.values())


def test_later_the_same_day_still_counts_as_newer():
    xml, original, update, way = scene()
    dated(original, "2026-09-25T09:00:00Z")
    dated(update, "2026-09-25T17:30:00Z")
    data, _, _, result, _ = run(xml, [original, update])
    assert [g[2] for g in pieces_of(data, result, way)][1] == {TECH_KEY: "4", EXPOSURE_KEY: "1"}
    assert result.ways[0].overlaps[0].rule == "newer"


def test_older_point_outside_the_newer_track_is_not_stale():
    """A newer track rating the same way alike merges into one piece with the
    older one; a point of the older track where the newer never went keeps
    its node."""
    xml, original, update, _way = scene(
        original_points=[exposure_point(100, 2), exposure_point(300, 2)],
        update_rating=(2, 1),
    )
    data, _, _, _result, points = run(xml, [original, update])
    far, inside = points.changes
    assert far.status == "tagged", far.reason
    assert inside.status == "skipped" and inside.reason.startswith("stale:")


def test_report_flags_a_node_rated_above_its_way(tmp_path):
    """The fixture lets a point outrank its segment; the report must say so."""
    xml, original, _update, _way = scene(original_points=[exposure_point(100, 2)])
    md = report_for(xml, [original], tmp_path)
    section = md.split("### Exposure nodes above their way's rating")[1].split("###")[0]
    assert "is rated 2 (Original / S001)" in section and "which has exposure 1" in section


def test_no_split_where_a_track_gap_falls_on_an_already_rated_way():
    """Production already carries (2,1) on the whole way; two tracks rate its
    ends alike and leave the middle uncovered. A rerun must not cut it."""
    net = Network()
    path = legs((0, 0), [(250, 60), (350, 95)])
    nodes = points_along(path, [float(d) for d in range(0, 601, 35)] + [600.0])
    way = net.polyline(nodes, {**PATH_TAGS, "name": "Test Trail", TECH_KEY: "2", EXPOSURE_KEY: "1"})
    first = [point_along(path, float(d)) for d in range(0, 201, 10)]
    last = [point_along(path, float(d)) for d in range(300, 601, 10)]
    a = dated(itrs_export_along(net, first, [_seg(200, 2, 1)], track_name="A"), OLD)
    b = dated(itrs_export_along(net, last, [_seg(300, 2, 1)], track_name="B"), NEW)
    data, _, _, result, _ = run(net.to_xml(), [a, b])
    (change,) = result.ways
    assert change.status == "unchanged", (change.cuts, change.notes)
    assert not change.cuts and data.ways[way].action is None
    assert all(w.id > 0 for w in data.ways.values())


def split_scene(*, existing_value: str | None, point_rating: int = 2):
    """Production state: one trail already split into two ways at 300 m. The
    track's second segment starts at 290 m, so its first 10 m match as a
    tail onto the first way and get dropped; its point sits at 295 m."""
    net = Network()
    path = legs((0, 0), [(250, 60), (350, 95)])
    tags = {**PATH_TAGS, "name": "Test Trail"}
    a_pts = [point_along(path, float(d)) for d in [*range(0, 281, 35), 295, 300]]
    way_a = net.polyline(a_pts, {**tags, TECH_KEY: "2", EXPOSURE_KEY: "1"})
    junction = net.way_nodes(way_a)[-1]
    b_pts = [junction, *(point_along(path, float(d)) for d in [*range(335, 600, 35), 600])]
    way_b = net.polyline(b_pts, {**tags, TECH_KEY: "3", EXPOSURE_KEY: "2"})
    node_295 = net.way_nodes(way_a)[-2]
    export = dated(
        itrs_export_along(
            net,
            path,
            [_seg(290, 2, 1), _seg(310, 3, 2)],
            track_name="Rerun",
            points=[exposure_point(295, point_rating)],
        ),
        NEW,
    )
    xml = net.to_xml()
    data = read_osm_xml(xml)
    if existing_value is not None:
        data.nodes[node_295].tags[EXPOSURE_KEY] = existing_value
    return data, export, way_a, way_b, node_295


def run_data(data, exports, *, force=False):
    tracks = [parse_track(e, source="x") for e in exports]
    matches = [match_track(t, data, MatchOptions()) for t in tracks]
    result = apply_matches(data, matches, CutOptions(force=force))
    return result, apply_points(data, matches, result, PointOptions(force=force))


def test_point_on_a_dropped_tail_of_the_same_trail_gets_its_node():
    data, export, way_a, _way_b, node_295 = split_scene(existing_value=None)
    _result, points = run_data(data, [export])
    (change,) = points.changes
    assert change.status == "tagged", change.reason
    assert change.way_id == way_a and change.node_id == node_295
    assert data.nodes[node_295].tags[EXPOSURE_KEY] == "2"


def test_point_on_a_tail_onto_another_trail_stays_unplaced():
    """Only the second trail is surveyed; its first 10 m overshoot onto the
    first way, which no segment keeps. Same name: the node goes there (one
    trail cut in two). Another name: a junction overshoot, no node."""
    for other_name, status in (("Test Trail", "tagged"), ("Other Trail", "unplaced")):
        data, _export, way_a, _way_b, _node = split_scene(existing_value=None)
        data.ways[way_a].tags["name"] = other_name
        net = Network()
        path = legs((0, 0), [(250, 60), (350, 95)])
        sub = [point_along(path, float(d)) for d in range(290, 601, 10)]
        export = itrs_export_along(
            net, sub, [_seg(310, 3, 2)], track_name="Second", points=[exposure_point(0, 2)]
        )
        _result, points = run_data(data, [export])
        (change,) = points.changes
        assert change.status == status, (other_name, change.reason)
        if status == "unplaced":
            assert "a different trail" in change.reason


def test_point_the_match_cannot_place_is_found_by_location(monkeypatch):
    import itrs_to_osm.points as points_module

    data, export, _way_a, _way_b, node_295 = split_scene(existing_value="2")
    monkeypatch.setattr(points_module, "_locate", lambda *a, **k: "its track vertex is off-network")
    _result, points = run_data(data, [export])
    (change,) = points.changes
    assert change.status == "already", change.reason
    assert change.node_id == node_295 and "found by location" in change.reason
    assert data.nodes[node_295].action is None


def test_point_found_by_location_with_a_different_value_is_refused_unless_forced(monkeypatch):
    import itrs_to_osm.points as points_module

    monkeypatch.setattr(points_module, "_locate", lambda *a, **k: "its track vertex is off-network")
    data, export, _way_a, _way_b, node_295 = split_scene(existing_value="3")
    _result, points = run_data(data, [export])
    (change,) = points.changes
    assert change.status == "refused" and change.old == "3" and points.refusals
    assert data.nodes[node_295].tags[EXPOSURE_KEY] == "3"
    data, export, _way_a, _way_b, node_295 = split_scene(existing_value="3")
    _result, points = run_data(data, [export], force=True)
    (change,) = points.changes
    assert change.status == "overwritten" and change.old == "3"
    assert data.nodes[node_295].tags[EXPOSURE_KEY] == "2"


def test_large_edit_warning(tmp_path, monkeypatch):
    import itrs_to_osm.report as report_module

    xml, original, update, _way = scene(original_points=[exposure_point(100, 2)])
    md = report_for(xml, [original, update], tmp_path)
    assert "Large edit:" not in md
    monkeypatch.setattr(report_module, "LARGE_EDIT_ELEMENTS", 1)
    md = report_for(xml, [original, update], tmp_path)
    assert "**Warnings:**" in md
    assert "Large edit: this run changes or creates" in md
    assert "Automated_Edits_code_of_conduct" in md and "mechanical=yes" in md


def test_survey_newer_than_the_osm_edit_overwrites_without_force():
    """Production carries (3,1) on the whole way from an upload in May; the
    export is a September survey, so its (2,1) replaces it. A way edited
    after the survey is refused as before."""
    for edited, expect_status in (
        ("2026-05-01T12:00:00Z", "tagged"),
        ("2026-10-01T12:00:00Z", "refused"),
    ):
        net = Network()
        path = legs((0, 0), [(150, 80), (150, 100), (100, 70)])
        way = net.polyline(path, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=20)
        export = dated(itrs_export_along(net, path, [_seg(400, 2, 1)], track_name="X"), NEW)
        data = read_osm_xml(net.to_xml())
        w = data.ways[way]
        w.tags[TECH_KEY], w.tags[EXPOSURE_KEY], w.timestamp = "3", "1", edited
        result, _points = run_data(data, [export])
        (change,) = result.ways
        assert change.status == expect_status, (edited, change.notes, change.refusals)
        if expect_status == "tagged":
            assert data.ways[way].tags[TECH_KEY] == "2"
            assert any(
                "is newer than the way's last OSM edit (2026-05-01)" in n for n in change.notes
            )
        else:
            assert data.ways[way].tags[TECH_KEY] == "3" and result.refusals


def test_exposure_node_newer_survey_overwrites_without_force():
    for edited, expect_status in (
        ("2026-05-01T12:00:00Z", "overwritten"),
        ("2026-10-01T12:00:00Z", "refused"),
    ):
        data, export, _way_a, _way_b, node_295 = split_scene(existing_value="3")
        data.nodes[node_295].timestamp = edited
        _result, points = run_data(data, [export])
        (change,) = points.changes
        assert change.status == expect_status, (edited, change.reason)
        assert data.nodes[node_295].tags[EXPOSURE_KEY] == (
            "2" if expect_status == "overwritten" else "3"
        )
        if expect_status == "overwritten":
            assert "newer than the node's last OSM edit (2026-05-01)" in change.reason


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
