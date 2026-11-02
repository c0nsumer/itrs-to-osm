"""Exposure points as tagged nodes (points.py) over the synthetic scenarios."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET

import pytest
from conftest import point_feature
from fixtures.builder import SCENARIOS, exposure_point

from itrs_to_osm.itrs import distance_m, parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import read_osm_xml, to_josm_xml
from itrs_to_osm.points import PointOptions, apply_points
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY


def pipeline(name: str = "exposure_points", *, export=None, force: bool = False, **opts):
    xml, scenario_export, expect = SCENARIOS[name]()
    track = parse_track(export if export is not None else scenario_export, source=name)
    data = read_osm_xml(xml)
    original = read_osm_xml(xml)
    matches = [match_track(track, data, MatchOptions())]
    result = apply_matches(data, matches, CutOptions(force=force))
    points = apply_points(data, matches, result, PointOptions(force=force, **opts))
    return data, original, result, points, expect


def snapshot(data):
    return (
        {n.id: (dict(n.tags), n.lat, n.lon, n.action) for n in data.nodes.values()},
        {w.id: (list(w.nodes), dict(w.tags), w.action) for w in data.ways.values()},
    )


def way_holding(data, node_id):
    return [w for w in data.ways.values() if node_id in w.nodes and w.tags.get("highway")]


@pytest.mark.parametrize("force", [False, True])
def test_exposure_points_ground_truth(force):
    data, original, _result, points, expect = pipeline(force=force)
    want = expect["exposure_nodes"]
    assert len(points.changes) == len(want)
    for change, w in zip(points.changes, want):
        label = w["label"]
        status = w["status_with_force"] if force else w["status"]
        assert change.status == status, (label, change.status, change.reason)
        assert change.value == w["rating"], label
        assert change.kind == w["kind"], label
        if w["kind"] == "node":
            assert change.node_id == w["node_id"], label
            assert change.moved_m <= 5.0
        elif w["kind"] == "inserted":
            assert change.node_id < 0, label
            (way,) = way_holding(data, change.node_id)
            assert change.way_id == way.id
            k = way.nodes.index(change.node_id)
            assert (way.nodes[k - 1], way.nodes[k + 1]) == w["between"], label
            node = data.nodes[change.node_id]
            assert node.version is None
            assert node.tags == {EXPOSURE_KEY: str(w["rating"])}, label
        else:
            assert change.node_id is None, label
        if w["old"] is not None:
            assert change.old == w["old"], label
        node_tags = data.nodes[change.node_id].tags if change.node_id else None
        if status in ("tagged", "overwritten"):
            assert node_tags == {EXPOSURE_KEY: str(w["rating"])}, label
            assert data.nodes[change.node_id].action == "modify" or change.node_id < 0
        elif status == "refused":
            assert node_tags == {EXPOSURE_KEY: w["old"]}, label
            assert data.nodes[change.node_id].action is None
        if w["not_node"] is not None:
            assert change.node_id != w["not_node"]

    # Point c: the junction node is untouched, the new node sits ~1 m from it.
    junction = expect["junction_node"]
    assert data.nodes[junction].tags == original.nodes[junction].tags == {}
    assert data.nodes[junction].action is None
    c = next(ch for ch, w in zip(points.changes, want) if w["label"] == "c")
    jn, new = data.nodes[junction], data.nodes[c.node_id]
    assert 0.9 <= distance_m((jn.lon, jn.lat), (new.lon, new.lat)) <= 1.5
    assert c.moved_m == pytest.approx(1.0, abs=0.2)

    # Point e lands on the piece split off at 450 m, not the keeper.
    e = next(ch for ch, w in zip(points.changes, want) if w["label"] == "e")
    assert e.way_id < 0 and expect["tagged_node_id"] in data.ways[e.way_id].nodes

    # No node gained or lost anything but the exposure key.
    for node in data.nodes.values():
        extra = {k: v for k, v in node.tags.items() if k != EXPOSURE_KEY}
        before = original.nodes[node.id].tags if node.id in original.nodes else {}
        assert extra == {k: v for k, v in before.items() if k != EXPOSURE_KEY}, node.id

    assert points.nodes_added == expect["points_nodes_added"]
    refused = [c for c in points.changes if c.status == "refused"]
    assert points.refusals == refused
    assert len(points.written) == sum(
        (w["status_with_force"] if force else w["status"]) in ("tagged", "overwritten")
        for w in want
    )
    # The side path never changes.
    side = expect["ways"]["side"]
    assert data.ways[side].nodes == original.ways[side].nodes
    assert data.ways[side].action is None


def test_refused_point_flips_to_overwritten_with_force():
    _, _, _, points, expect = pipeline()
    tagged_node = expect["tagged_node_id"]
    (refused,) = points.refusals
    assert refused.node_id == tagged_node and refused.old == "3"
    assert "survey says 2" in refused.reason
    data, _, _, points, _ = pipeline(force=True)
    assert not points.refusals
    over = next(c for c in points.changes if c.node_id == tagged_node)
    assert over.status == "overwritten" and over.old == "3"
    assert data.nodes[tagged_node].tags == {EXPOSURE_KEY: "2"}
    assert data.nodes[tagged_node].action == "modify"


def test_disabled_skips_everything_without_mutation():
    xml, export, _ = SCENARIOS["exposure_points"]()
    track = parse_track(export)
    data = read_osm_xml(xml)
    matches = [match_track(track, data)]
    result = apply_matches(data, matches)
    before = snapshot(data)
    points = apply_points(data, matches, result, PointOptions(enabled=False))
    assert snapshot(data) == before
    assert points.changes and all(c.status == "skipped" for c in points.changes)
    assert all("--no-exposure-nodes" in c.reason for c in points.changes)
    assert points.nodes_added == 0 and not points.written


def test_min_exposure_skips_lower_ratings():
    data, _, _, points, expect = pipeline(min_exposure=3)
    for change, w in zip(points.changes, expect["exposure_nodes"]):
        if w["rating"] < 3:
            assert change.status == "skipped", w["label"]
            assert "below --exposure-min 3" in change.reason
        else:
            assert change.status == "tagged", w["label"]
    assert points.nodes_added == 1
    # The node already carrying exposure=3 is left alone: no refusal.
    assert not points.refusals
    assert data.nodes[expect["tagged_node_id"]].action is None


def _with_point(name: str, coords_of, segment_uuid_of=None, **props):
    """The scenario's export plus one Exposure point built from its segments."""
    _, export, _ = SCENARIOS[name]()
    export = copy.deepcopy(export)
    segs = [f for f in export["features"] if f["geometry"]["type"] == "LineString"]
    seg = segs[-1]
    spec = exposure_point(0.0, 3)
    uuid = segment_uuid_of(seg) if segment_uuid_of else seg["properties"]["segment_uuid"]
    feature = point_feature(
        "Exposure",
        list(coords_of(seg)),
        uuid,
        point_segment_name=seg["properties"]["segment_name"],
        **{**spec["props"], **props},
    )
    export["features"].append(feature)
    return export


def test_point_on_dropped_tail_is_unplaced():
    export = _with_point("tail_onto_other_way", lambda seg: seg["geometry"]["coordinates"][-1])
    data, original, _, points, expect = pipeline("tail_onto_other_way", export=export)
    (change,) = points.changes
    assert change.status == "unplaced"
    assert "dropped tail" in change.reason
    assert str(expect["ways"]["other"]) in change.reason
    assert change.node_id is None and points.nodes_added == 0
    other = expect["ways"]["other"]
    assert data.ways[other].nodes == original.ways[other].nodes


def test_point_with_unknown_segment_is_unplaced():
    export = _with_point(
        "single_way_uniform",
        lambda seg: seg["geometry"]["coordinates"][10],
        segment_uuid_of=lambda seg: "-NOPE",
    )
    _, _, _, points, _ = pipeline("single_way_uniform", export=export)
    (change,) = points.changes
    assert change.status == "unplaced"
    assert "unknown segment -NOPE" in change.reason


def test_josm_output_carries_the_nodes():
    data, _, _, points, expect = pipeline()
    root = ET.fromstring(to_josm_xml(data, generator="test"))
    nodes = {int(n.get("id")): n for n in root.findall("node")}

    def tags(el):
        return {t.get("k"): t.get("v") for t in el.findall("tag")}

    a = next(c for c, w in zip(points.changes, expect["exposure_nodes"]) if w["label"] == "a")
    existing = nodes[a.node_id]
    assert existing.get("action") == "modify"
    assert existing.get("version") == "1"
    assert tags(existing) == {EXPOSURE_KEY: "2"}

    b = next(c for c, w in zip(points.changes, expect["exposure_nodes"]) if w["label"] == "b")
    assert b.node_id < 0
    inserted = nodes[b.node_id]
    assert inserted.get("version") is None
    assert tags(inserted) == {EXPOSURE_KEY: "3"}
    # The way holding it references it.
    holders = [w for w in root.findall("way") if any(nd.get("ref") == str(b.node_id) for nd in w)]
    assert len(holders) == 1 and holders[0].get("action") == "modify"

    # The refused node is written unchanged.
    refused = nodes[expect["tagged_node_id"]]
    assert refused.get("action") is None
    assert tags(refused) == {EXPOSURE_KEY: "3"}


def test_boundary_scenario_point_is_tagged_with_realistic_props():
    _, export, _ = SCENARIOS["boundary_near_existing_node"]()
    (point,) = parse_track(export).points
    assert point.point_type == "Exposure" and point.value == 2
    assert point.props["point_exposure_length"] == ""
    assert point.props["point_exposure_width_of_trail_bed_cm"] == "40"
    assert point.props["point_exposure_off_camber_deg"] == "8.0"
    data, _, _, points, _ = pipeline("boundary_near_existing_node")
    (change,) = points.changes
    assert change.status == "tagged"
    assert data.nodes[change.node_id].tags == {EXPOSURE_KEY: "2"}


def _exposure_points_with_extra(rating: int, base_label: str, base_rating: int | None = None):
    """exposure_points plus a second Exposure point on the same vertex as ``base_label``.

    ``base_rating`` re-rates the original point first.
    """
    _, export, expect = SCENARIOS["exposure_points"]()
    export = copy.deepcopy(export)
    exposure = [f for f in export["features"] if f["properties"].get("point_type") == "Exposure"]
    base = exposure[[w["label"] for w in expect["exposure_nodes"]].index(base_label)]
    if base_rating is not None:
        base["properties"]["point_exposure_rating"] = base_rating
    extra = copy.deepcopy(base)
    extra["properties"]["point_exposure_rating"] = rating
    export["features"].append(extra)
    return export


def test_two_points_on_one_node_keep_the_higher_rating():
    # Point b (rated 3) inserts a node; a second point rated 4 on the same vertex
    # snaps to that node and raises it.
    export = _exposure_points_with_extra(4, "b")
    data, _, _, points, _ = pipeline(export=export)
    b, extra = points.changes[1], points.changes[-1]
    assert extra.node_id == b.node_id
    assert extra.status == "tagged" and b.status == "skipped"
    assert "higher rating stays" in b.reason
    assert data.nodes[b.node_id].tags == {EXPOSURE_KEY: "4"}
    assert points.nodes_added == 2  # still one node per placed spot


def test_higher_second_point_does_not_bypass_a_refusal():
    # BUG (points.py, apply_points, the `earlier` branch): node 517 already has
    # exposure=3 and point e (2) is refused. A second point rated 4 on the same
    # node is compared with the node's current value, not checked against the
    # refusal policy, so it overwrites 3 -> 4 without --force.
    export = _exposure_points_with_extra(4, "e")
    data, _, _, points, expect = pipeline(export=export)
    node = data.nodes[expect["tagged_node_id"]]
    extra = points.changes[-1]
    assert extra.node_id == node.id
    assert extra.status == "refused", (extra.status, extra.reason)
    assert node.tags == {EXPOSURE_KEY: "3"}


def test_higher_second_point_marks_the_node_modified():
    # BUG (points.py, apply_points, the `earlier` branch): node 517 has
    # exposure=3 and point e, re-rated 3, is "already" (node not marked). A
    # second point rated 4 raises the tag to 4 but never calls mark_modified,
    # so the .osm carries a changed node without action="modify".
    export = _exposure_points_with_extra(4, "e", base_rating=3)
    data, _, _, points, expect = pipeline(export=export, force=True)
    node = data.nodes[expect["tagged_node_id"]]
    assert points.changes[4].status == "skipped"  # e (3) gives way to the 4
    assert points.changes[-1].status in ("tagged", "overwritten")
    assert node.tags == {EXPOSURE_KEY: "4"}
    assert node.action == "modify"
