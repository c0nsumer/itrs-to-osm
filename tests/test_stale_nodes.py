"""Stale exposure nodes: a re-survey that no longer has a point for a node.

Production already carries exposure nodes from an earlier upload. A newer
survey of the same stretch with fewer or different points must take the tag
off the nodes it no longer supports, by the same date rule as a differing
value, and must leave everything it didn't survey alone.
"""

from __future__ import annotations

import json

from fixtures.builder import Network, _seg, exposure_point, itrs_export_along
from test_resurvey import NEW, OLD, PATH_TAGS, dated, run, scene

import itrs_to_osm.points as points_module
from itrs_to_osm.cli import EXIT_OK, EXIT_REFUSED, main
from itrs_to_osm.itrs import parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import read_osm_file, read_osm_xml, write_josm_file
from itrs_to_osm.points import PointOptions, apply_points
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY

BEFORE, AFTER = "2026-05-01T12:00:00Z", "2026-10-01T12:00:00Z"  # either side of NEW
PATH = [(0.0, 0.0), (400.0, 0.0)]


def trail(*, survey_m=400.0, exposure=2, points=(), tagged=((100, "3", BEFORE),)):
    """A straight 400 m way with a node every 20 m, surveyed from 0 m to
    ``survey_m`` in September as one segment rated (2, ``exposure``).
    ``tagged``: (at_m, value, last edit) of nodes that carry the tag already."""
    net = Network()
    way = net.polyline(PATH, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=20)
    export = dated(
        itrs_export_along(
            net,
            [(0.0, 0.0), (survey_m, 0.0)],
            [_seg(survey_m, 2, exposure)],
            track_name="Resurvey",
            points=list(points),
        ),
        NEW,
    )
    data = read_osm_xml(net.to_xml())
    nodes = {}
    for at_m, value, edited in tagged:
        node = data.nodes[data.ways[way].nodes[round(at_m / 20)]]
        node.tags[EXPOSURE_KEY], node.timestamp = value, edited
        nodes[at_m] = node
    return data, export, way, nodes


def apply(data, export, **options):
    track = parse_track(export, source="x")
    matches = [match_track(track, data, MatchOptions())]
    result = apply_matches(data, matches, CutOptions(force=options.get("force", False)))
    return apply_points(data, matches, result, PointOptions(**options))


def test_node_the_survey_dropped_loses_its_tag():
    data, export, way, nodes = trail(points=[exposure_point(300, 2)])
    points = apply(data, export)
    (stale,) = points.stale
    node = nodes[100]
    assert stale.status == "removed" and stale.node_id == node.id and stale.old == "3"
    assert "newer than the node's last OSM edit (2026-05-01)" in stale.reason
    assert stale.sources == ["Resurvey / S001"]
    assert EXPOSURE_KEY not in node.tags and node.action == "modify"
    assert node.id in data.ways[way].nodes  # the node stays, only the tag goes
    (fresh,) = points.written
    assert data.nodes[fresh.node_id].tags[EXPOSURE_KEY] == "2"


def test_node_edited_after_the_survey_is_refused_unless_forced():
    for edited in (AFTER, None):
        data, export, _way, nodes = trail(tagged=[(100, "3", edited)])
        points = apply(data, export)
        (stale,) = points.stale_refusals
        assert stale.node_id == nodes[100].id and not points.removed
        assert nodes[100].tags[EXPOSURE_KEY] == "3" and nodes[100].action is None

        data, export, _way, nodes = trail(tagged=[(100, "3", edited)])
        points = apply(data, export, force=True)
        (stale,) = points.removed
        assert "--force" in stale.reason and EXPOSURE_KEY not in nodes[100].tags


def test_node_the_survey_still_has_a_point_for_is_not_stale():
    data, export, _way, nodes = trail(points=[exposure_point(100, 3)])
    points = apply(data, export)
    assert not points.stale
    assert [c.status for c in points.changes] == ["already"]
    assert nodes[100].tags[EXPOSURE_KEY] == "3" and nodes[100].action is None


def test_point_below_exposure_min_still_backs_its_node():
    data, export, _way, nodes = trail(points=[exposure_point(100, 2)], tagged=[(100, "2", BEFORE)])
    points = apply(data, export, min_exposure=3)
    (change,) = points.changes
    assert change.status == "skipped" and change.node_id is None
    assert change.backs_node_id == nodes[100].id
    assert not points.stale and nodes[100].tags[EXPOSURE_KEY] == "2"


def test_node_past_the_end_of_the_survey_is_left_alone():
    data, export, _way, nodes = trail(
        survey_m=200.0, tagged=[(100, "3", BEFORE), (300, "3", BEFORE)]
    )
    points = apply(data, export)
    assert [s.node_id for s in points.removed] == [nodes[100].id]
    assert nodes[300].tags[EXPOSURE_KEY] == "3" and nodes[300].action is None


def test_segment_without_an_exposure_rating_removes_nothing():
    data, export, _way, nodes = trail(exposure=0)
    points = apply(data, export)
    assert not points.stale and nodes[100].tags[EXPOSURE_KEY] == "3"


def test_unplaced_point_holds_its_segment(monkeypatch):
    data, export, _way, nodes = trail(points=[exposure_point(300, 2)])
    monkeypatch.setattr(points_module, "_locate", lambda *a, **k: "its track vertex is off-network")
    points = apply(data, export)
    assert [c.status for c in points.changes] == ["unplaced"]
    (stale,) = points.stale
    assert stale.status == "kept" and "couldn't be placed" in stale.reason
    assert nodes[100].tags[EXPOSURE_KEY] == "3" and nodes[100].action is None


def test_junction_with_a_way_outside_the_survey_is_left_alone():
    net = Network()
    way = net.polyline(PATH, {**PATH_TAGS, "name": "Test Trail"}, spacing_m=20)
    data = read_osm_xml(net.to_xml())
    junction = data.ways[way].nodes[5]
    net.polyline([junction, (100.0, 150.0)], {**PATH_TAGS, "name": "Side Trail"})
    export = dated(itrs_export_along(net, PATH, [_seg(400, 2, 2)], track_name="Resurvey"), NEW)
    data = read_osm_xml(net.to_xml())
    data.nodes[junction].tags[EXPOSURE_KEY] = "3"
    data.nodes[junction].timestamp = BEFORE
    points = apply(data, export)
    assert not points.stale and data.nodes[junction].tags[EXPOSURE_KEY] == "3"


def test_switches_turn_the_step_off():
    for options in ({"remove_stale": False}, {"enabled": False}):
        data, export, _way, nodes = trail()
        points = apply(data, export, **options)
        assert not points.stale and nodes[100].tags[EXPOSURE_KEY] == "3"


def test_newer_survey_in_a_batch_decides(tmp_path):
    """The June survey has a point at 300 m and the node is in OSM. The
    September update re-rates 250-350 m with no point: the node goes, even
    though the older export in the same batch still carries the point."""
    xml, original, update, way = scene(original_points=[exposure_point(300, 2)])
    data = read_osm_xml(xml)
    node_id = data.ways[way].nodes[8]  # at 280 m, inside the update's 250-350 m
    data.nodes[node_id].tags[EXPOSURE_KEY] = "2"
    data.nodes[node_id].timestamp = OLD
    tagged = tmp_path / "tagged.osm"
    write_josm_file(data, tagged, generator="test")
    data, _tracks, _matches, _result, points = run(tagged.read_text("utf-8"), [original, update])
    assert [(c.track_name, c.status) for c in points.changes] == [("Original", "skipped")]
    (stale,) = points.removed
    assert stale.node_id == node_id and stale.sources == ["Update / S001"]
    assert EXPOSURE_KEY not in data.nodes[node_id].tags


def test_cli_refuses_then_forces_then_keeps(tmp_path, capsys):
    data, export, _way, nodes = trail(tagged=[(100, "3", AFTER)])
    osm = tmp_path / "area.osm"
    write_josm_file(data, osm, generator="test")
    src = tmp_path / "geojson-export_Resurvey.geojson"
    src.write_text(json.dumps(export), encoding="utf-8")
    node_id = nodes[100].id
    args = [str(src), "--osm", str(osm), "--html"]

    assert main(args) == EXIT_REFUSED
    assert "1 stale exposure node(s) edited since the survey" in capsys.readouterr().err
    md = (tmp_path / "Resurvey.report.md").read_text(encoding="utf-8")
    assert "Refusals: 1" in md and f"n{node_id}" in md
    assert "Refused, tag left in place" in md
    assert EXPOSURE_KEY in read_osm_file(tmp_path / "Resurvey.osm").nodes[node_id].tags

    assert main([*args, "--force"]) == EXIT_OK
    assert "1 stale untagged" in capsys.readouterr().out
    out = read_osm_file(tmp_path / "Resurvey.osm")
    assert EXPOSURE_KEY not in out.nodes[node_id].tags and out.nodes[node_id].action == "modify"
    assert out.ways and all(TECH_KEY in w.tags for w in out.ways.values())
    md = (tmp_path / "Resurvey.report.md").read_text(encoding="utf-8")
    assert "Tag removed: forced (--force)" in md and "Look at each stale node" in md
    assert "Stale exposure node n" in (tmp_path / "Resurvey.report.html").read_text("utf-8")

    assert main([*args, "--keep-stale-exposure-nodes"]) == EXIT_OK
    md = (tmp_path / "Resurvey.report.md").read_text(encoding="utf-8")
    assert "not checked (--keep-stale-exposure-nodes)" in md
    assert EXPOSURE_KEY in read_osm_file(tmp_path / "Resurvey.osm").nodes[node_id].tags
