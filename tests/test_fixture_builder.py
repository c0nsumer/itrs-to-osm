"""Checks on the synthetic fixture builder itself (tests/fixtures/builder.py)."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

import pytest
from fixtures.builder import (
    EXPOSURE,
    SCENARIOS,
    TECH,
    Network,
    cumulative_m,
    itrs_export_along,
    legs,
    points_along,
)

from itrs_to_osm.itrs import distance_m, parse_track

META_ATTRS = ("id", "version", "timestamp", "changeset", "uid", "user")


def _segment_features(export: dict) -> list[dict]:
    return [f for f in export["features"] if f["geometry"]["type"] == "LineString"]


@pytest.fixture(params=sorted(SCENARIOS))
def scenario(request):
    return request.param, SCENARIOS[request.param]()


def test_scenario_export_parses_cleanly(scenario):
    _name, (_xml, export, expect) = scenario
    track = parse_track(export)
    assert len(track.warnings) == expect["parse_warnings"], track.warnings
    assert [s.position for s in track.segments] == list(range(1, len(track.segments) + 1))
    assert [s.name for s in track.segments] == [
        f"S{i:03d}" for i in range(1, len(track.segments) + 1)
    ]
    assert track.start == track.segments[0].coords[0]
    assert track.length_m == pytest.approx(
        sum(s.geometry_length_m() for s in track.segments), abs=1
    )


def test_scenario_segments_contiguous_and_lengths_match(scenario):
    _name, (_xml, export, _expect) = scenario
    track = parse_track(export)
    for seg in track.segments:
        assert all(len(c) == 3 and c[2] is not None for c in seg.coords)
        assert abs(seg.length_m - seg.geometry_length_m()) <= 1.0
        assert seg.length_m == round(seg.geometry_length_m())
    for prev, cur in zip(track.segments, track.segments[1:]):
        assert prev.coords[-1] == cur.coords[0]


def test_scenario_xml_well_formed_with_meta(scenario):
    _name, (xml, _export, expect) = scenario
    root = ET.fromstring(xml)
    assert root.tag == "osm" and root.get("version") == "0.6"
    assert root.find("meta").get("osm_base")
    assert root.find("bounds") is not None
    elements = root.findall("node") + root.findall("way") + root.findall("relation")
    for element in elements:
        for attr in META_ATTRS:
            assert element.get(attr), (element.tag, attr)
        assert element.get("version") == "1"
        assert element.get("user") == "fixture"
    node_ids = {int(n.get("id")) for n in root.findall("node")}
    way_ids = {int(w.get("id")) for w in root.findall("way")}
    for way in root.findall("way"):
        assert all(int(nd.get("ref")) in node_ids for nd in way.findall("nd"))
    assert way_ids == set(expect["way_nodes_m"])
    for role_id in list(expect["ways"].values()) + expect["trail_way_ids"]:
        assert role_id in way_ids
    rel_ids = {int(r.get("id")) for r in root.findall("relation")}
    assert rel_ids == set(expect["relations"].values())


def test_scenario_expect_is_consistent(scenario):
    _name, (_xml, export, expect) = scenario
    for key in (
        "trail_way_ids",
        "untouched_way_ids",
        "cuts",
        "pieces",
        "unmatched_segments",
        "not_applied",
        "not_rated",
        "dropped_tails",
        "refused",
        "parse_warnings",
        "way_nodes_m",
    ):
        assert key in expect
    junctions = {
        node
        for way, nodes in expect["way_nodes_m"].items()
        for node, _ in nodes
        if sum(node in {n for n, _ in other} for other in expect["way_nodes_m"].values()) > 1
    }
    for way in expect["trail_way_ids"]:
        assert way in expect["cuts"] and way in expect["pieces"]
        nodes = expect["way_nodes_m"][way]
        length = nodes[-1][1]
        pieces = expect["pieces"][way]
        assert pieces[0]["from_m"] == 0
        assert pieces[-1]["to_m"] == pytest.approx(length, abs=0.01)
        for a, b in zip(pieces, pieces[1:]):
            assert a["to_m"] == b["from_m"]
            assert a["tags"] != b["tags"]  # equal neighbors should have been merged
        assert [c["at_m"] for c in expect["cuts"][way]] == [p["from_m"] for p in pieces[1:]]
        for cut in expect["cuts"][way]:
            nearest_node, nearest_m = min(nodes, key=lambda pair: abs(pair[1] - cut["at_m"]))
            gap = abs(nearest_m - cut["at_m"])
            near_junction = any(
                abs(m - cut["at_m"]) <= 15 for n, m in nodes if n in junctions or m in (0, length)
            )
            if cut["kind"] == "snap":
                assert gap <= 5 and cut["node_id"] == nearest_node
            elif cut["kind"] == "insert":
                assert gap > 5 and not near_junction
            for piece in pieces:
                assert set(piece["tags"]) <= {TECH, EXPOSURE}
    assert not set(expect["trail_way_ids"]) & set(expect["untouched_way_ids"])
    names = {f["properties"]["segment_name"] for f in _segment_features(export)}
    assert set(expect["unmatched_segments"]) <= names
    assert set(expect["not_applied"]) <= names


def test_ids_allocated_from_ranges():
    net = Network()
    a, b = net.node(0, 0), net.node(10, 0)
    w = net.way([a, b], {"highway": "path"})
    r = net.relation([("way", w, "")], {"type": "route"})
    assert (a, b, w, r) == (1000, 1001, 5000, 9000)
    assert net.node(5, 5, id=42) == 42
    with pytest.raises(ValueError):
        net.node(0, 0, id=42)


def test_node_conversion_matches_haversine():
    net = Network()
    a, b, c = net.node(0, 0), net.node(300, 0), net.node(0, 400)
    assert net.node_lonlat(a) == pytest.approx((-87.80, 47.45))
    assert distance_m(net.node_lonlat(a), net.node_lonlat(b)) == pytest.approx(300, abs=0.05)
    assert distance_m(net.node_lonlat(a), net.node_lonlat(c)) == pytest.approx(400, abs=0.05)
    assert net.to_local(*net.node_lonlat(c)) == pytest.approx((0, 400), abs=0.01)


def test_polyline_densifies_and_reuses_nodes():
    net = Network()
    j = net.node(0, 0)
    w = net.polyline([j, (100, 0)], {"highway": "path"}, spacing_m=30)
    nodes = net.way_nodes(w)
    assert nodes[0] == j and len(nodes) == 5  # 4 edges of 25 m
    assert [round(m, 6) for _, m in net.way_nodes_m(w)] == [0, 25, 50, 75, 100]
    assert len(net.way_lonlat(w)) == 5


def test_xml_escapes_tags():
    net = Network()
    w = net.polyline([(0, 0), (10, 0)], {"name": 'Flo\'Rion & "Co" <x>'})
    root = ET.fromstring(net.to_xml())
    assert root.find("way/tag[@k='name']").get("v") == 'Flo\'Rion & "Co" <x>'
    assert int(root.find("way").get("id")) == w
    assert ET.fromstring(net.to_xml(bounds=False)).find("bounds") is None


def _straight_export(**kwargs):
    net = Network()
    path = legs((0, 0), [(200, 90)])
    segs = [
        {"length_m": 100, "tech": 2, "exposure": 1},
        {"length_m": 100, "tech": 3, "exposure": 1},
    ]
    return net, path, itrs_export_along(net, path, segs, **kwargs)


def _all_vertices(export):
    out = []
    for f in _segment_features(export):
        coords = f["geometry"]["coordinates"]
        out.extend(coords if not out else coords[1:])
    return out


def test_offset_moves_vertices_by_about_offset():
    _net, _path, plain = _straight_export()
    _net, _path, shifted = _straight_export(offset_m=4.0)
    a, b = _all_vertices(plain), _all_vertices(shifted)
    assert len(a) == len(b)
    for p, q in zip(a, b):
        assert distance_m(p, q) == pytest.approx(4.0, abs=0.05)
        assert q[1] > p[1]  # left of eastward travel is north


def test_jitter_is_deterministic_per_seed():
    _n, _p, one = _straight_export(jitter_m=2.0, seed=3)
    _n, _p, two = _straight_export(jitter_m=2.0, seed=3)
    _n, _p, other = _straight_export(jitter_m=2.0, seed=4)
    assert one == two
    assert one != other
    _n, _p, plain = _straight_export()
    for p, q in zip(_all_vertices(plain), _all_vertices(one)):
        assert distance_m(p, q) <= 2.0 + 0.05


def test_boundaries_land_exactly_on_vertices():
    net = Network()
    path = legs((0, 0), [(130, 45), (170, 120)])
    segs = [
        {"length_m": 37.3, "tech": 1, "exposure": 1},
        {"length_m": 101.9, "tech": 2, "exposure": 1},
        {"length_m": 500, "tech": 3, "exposure": 2},
    ]
    export = itrs_export_along(net, path, segs, spacing_m=8.0)
    feats = _segment_features(export)
    start = net.to_lonlat(*path[0])
    lengths = []
    for f in feats:
        coords = f["geometry"]["coordinates"]
        lengths.append(sum(distance_m(a, b) for a, b in zip(coords, coords[1:])))
        gaps = [distance_m(a, b) for a, b in zip(coords, coords[1:])]
        assert max(gaps) <= 8.0 + 0.05
        assert min(gaps) >= 2.0 - 0.05
    assert distance_m(start, feats[0]["geometry"]["coordinates"][0]) < 0.01
    assert lengths[0] == pytest.approx(37.3, abs=0.05)
    assert lengths[1] == pytest.approx(101.9, abs=0.05)
    # The path ends at 300 m, so the last segment is cut short.
    assert sum(lengths) == pytest.approx(cumulative_m(path)[-1], abs=0.1)
    end = net.to_lonlat(*path[-1])
    assert distance_m(end, feats[-1]["geometry"]["coordinates"][-1]) < 0.01


def test_segment_starting_past_path_end_rejected():
    net = Network()
    path = legs((0, 0), [(50, 0)])
    with pytest.raises(ValueError):
        itrs_export_along(
            net,
            path,
            [
                {"length_m": 60, "tech": 1, "exposure": 1},
                {"length_m": 10, "tech": 1, "exposure": 1},
            ],
        )


def test_44_style_and_points():
    net = Network()
    path = legs((0, 0), [(200, 90)])
    export = itrs_export_along(
        net,
        path,
        [{"length_m": 100, "tech": 2, "exposure": 1}, {"length_m": 100, "tech": 3, "exposure": 2}],
        style="4.4",
        track_name="Visp Test",
        points=[{"type": "Exposure", "at_m": 130.0, "props": {"point_exposure_rating": 2}}],
    )
    assert "version" not in export
    props = _segment_features(export)[0]["properties"]
    assert props["segment_trail_name"] == "Visp Test"
    track = parse_track(export)
    assert track.warnings == []
    assert track.name == "Visp Test"
    assert track.itrs_versions == ["4.4"]
    (point,) = track.points
    seg = track.segments[1]
    assert point.segment_uuid == seg.uuid and point.segment_name == "S002"
    assert (point.lon, point.lat, point.ele) in seg.coords
    assert point.value == 2
    assert track.max_tech_rating == 3 and track.max_exposure_rating == 2
    assert track.start_props["point_start_track_share_techLevel_2_perc"] == pytest.approx(50, abs=1)


def test_points_along_keeps_corners():
    path = legs((0, 0), [(100, 0), (100, 90)])
    pts = points_along(path, [0, 50, 150, 200])
    assert len(pts) == 5
    assert pts[2] == pytest.approx(path[1])
    assert math.dist(pts[3], (50, 100)) < 1e-6
