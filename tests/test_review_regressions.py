"""Regressions for the bugs an independent review found before the first JOSM run.

Each test builds a small network with the fixture builder and runs the
match + apply pipeline, then checks the output the way a reviewer in JOSM
would: complete, non-degenerate pieces in the right order with the right
tags, and relations that still chain.
"""

from __future__ import annotations

import pytest
from fixtures.builder import PATH_TAGS, Network, _seg, itrs_export_along

from itrs_to_osm.itrs import parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import read_osm_xml
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY, existing_itrs

TRAIL = {**PATH_TAGS, "name": "Test Trail"}


def run(net, tracks, **cut):
    """tracks: list of (name, path_m, segments). Returns (data, original, result)."""
    xml = net.to_xml()
    data, original = read_osm_xml(xml), read_osm_xml(xml)
    matches = []
    for name, path, segs in tracks:
        export = itrs_export_along(net, path, segs, track_name=name)
        matches.append(match_track(parse_track(export, source=name), data, MatchOptions()))
    return data, original, apply_matches(data, matches, CutOptions(**cut))


def check_integrity(data, original, result):
    """Every split reproduces the original node sequence; no piece is degenerate."""
    for w in result.ways:
        if w.status != "split":
            continue
        rebuilt: list[int] = []
        for p in sorted(w.pieces, key=lambda p: p.start_m):
            nodes = data.ways[p.way_id].nodes
            assert len(nodes) >= 2, (w.way_id, p)
            if rebuilt:
                assert rebuilt[-1] == nodes[0]
                rebuilt.extend(nodes[1:])
            else:
                rebuilt.extend(nodes)
        inserted = {c.node_id for c in w.cuts if c.kind == "inserted"}
        assert [n for n in rebuilt if n not in inserted] == original.ways[w.way_id].nodes
    for way in data.ways.values():
        assert len(way.nodes) >= 2, way


def tagged_pieces(result, way_id):
    change = next(w for w in result.ways if w.way_id == way_id)
    return [(round(p.start_m), round(p.end_m), existing_itrs(p.tags_after)) for p in change.pieces]


def test_interval_reaching_a_ways_last_node_covers_the_last_edge():
    """Finding 1: the stretch up to a way's end junction lost its last edge."""
    net = Network()
    w1 = net.polyline([(0, 0), (300, 0)], TRAIL, spacing_m=25)
    j = net.way_nodes(w1)[-1]
    w2 = net.polyline([j, (300, 250)], TRAIL, spacing_m=25)
    data, original, result = run(
        net, [("Test Trail", [(0, 0), (300, 0), (300, 250)], [_seg(550, 3, 1)])]
    )
    check_integrity(data, original, result)
    for way_id in (w1, w2):
        change = next(w for w in result.ways if w.way_id == way_id)
        assert change.status == "tagged", (way_id, change.status, change.cuts, change.notes)
        assert not change.cuts
        assert existing_itrs(data.ways[way_id].tags) == {TECH_KEY: "3", EXPOSURE_KEY: "1"}


def test_track_across_a_loops_seam_tags_only_what_was_ridden():
    """Finding 2: crossing the closing node of a loop tagged the far side instead."""
    net = Network()
    n0 = net.node(0, 0)
    loop = net.polyline([n0, (200, 0), (200, 200), (0, 200), n0], TRAIL, spacing_m=20)
    data, original, result = run(
        net, [("Test Trail", [(0, 150), (0, 0), (150, 0)], [_seg(300, 3, 1)])]
    )
    check_integrity(data, original, result)
    pieces = tagged_pieces(result, loop)
    tagged = [p for p in pieces if p[2]]
    untagged = [p for p in pieces if not p[2]]
    assert sum(b - a for a, b, _ in tagged) == pytest.approx(300, abs=12)
    assert len(untagged) == 1 and untagged[0][1] - untagged[0][0] == pytest.approx(500, abs=12)
    # The ridden stretches are the first 150 m and the last 150 m of the loop.
    assert min(a for a, _, _ in tagged) == 0
    assert max(b for _, b, _ in tagged) == pytest.approx(800, abs=1)


def test_overlap_leftover_near_the_way_start_makes_no_one_node_piece():
    """Finding 3: a 10 m leftover zone snapped its cut onto the way's first node."""
    net = Network()
    w = net.polyline([(0, 0), (400, 0)], TRAIL, spacing_m=25)
    data, original, result = run(
        net,
        [
            ("Trail A", [(0, 0), (200, 0)], [_seg(200, 2, 1)]),
            ("Trail B", [(10, 0), (400, 0)], [_seg(390, 3, 1)]),
        ],
    )
    check_integrity(data, original, result)
    change = result.ways[0]
    assert change.status == "tagged" and not change.cuts, (change.cuts, change.zones)
    assert existing_itrs(data.ways[w].tags) == {TECH_KEY: "3", EXPOSURE_KEY: "1"}
    assert any("dropped" in n for n in change.notes)
    assert all(way.id > 0 for way in data.ways.values())


@pytest.mark.parametrize("backward", [False, True])
def test_relation_direction_uses_the_original_way_ends(backward):
    """Finding 4: a middle keeper piece made the direction 'unknown' and the order wrong."""
    net = Network()
    a = net.polyline([(-300, 0), (0, 0)], PATH_TAGS, spacing_m=25)
    s = net.way_nodes(a)[-1]
    w = net.polyline([s, (600, 0)], TRAIL, spacing_m=25)
    e = net.way_nodes(w)[-1]
    b = net.polyline([e, (900, 0)], PATH_TAGS, spacing_m=25)
    members = [("way", a, ""), ("way", w, ""), ("way", b, "")]
    if backward:
        members.reverse()
    r = net.relation(members, {"type": "route", "route": "mtb", "name": "R"})
    data, original, result = run(net, [("Test Trail", [(150, 5), (450, 5)], [_seg(300, 3, 1)])])
    check_integrity(data, original, result)
    change = next(c for c in result.ways if c.way_id == w)
    assert change.status == "split" and len(change.pieces) == 3
    keeper = next(p for p in change.pieces if p.keeps_id)
    assert keeper.start_m == pytest.approx(150, abs=6) and keeper.end_m == pytest.approx(450, abs=6)
    rc = change.relations[0]
    assert rc.direction == ("backward" if backward else "forward") and rc.warning is None
    by_start = sorted(change.pieces, key=lambda p: p.start_m)
    expected = [a, by_start[0].way_id, w, by_start[2].way_id, b]
    if backward:
        expected.reverse()
    assert [m.ref for m in data.relations[r].members] == expected


def test_two_cuts_cannot_collapse_onto_one_junction_node():
    """Finding 5: both edges of a 20 m zone snapped to the same junction and the zone vanished."""
    net = Network()
    w = net.polyline([(0, 0), (110, 0), (400, 0)], TRAIL, spacing_m=25)
    j = next(n for n in net.way_nodes(w) if net.node_xy(n) == (110.0, 0.0))
    net.polyline([j, (110, 200)], PATH_TAGS, spacing_m=25)
    data, original, result = run(
        net,
        [("Test Trail", [(0, 0), (400, 0)], [_seg(100, 2, 1), _seg(20, 4, 3), _seg(280, 2, 1)])],
    )
    check_integrity(data, original, result)
    change = next(c for c in result.ways if c.way_id == w)
    assert len({c.node_id for c in change.cuts}) == len(change.cuts) == 2
    tags = [p[2].get(TECH_KEY) for p in tagged_pieces(result, w)]
    assert tags == ["2", "4", "2"], tagged_pieces(result, w)
    assert not any("did not land" in n for n in change.notes)


def test_cut_on_a_revisited_node_uses_the_right_occurrence():
    """Finding 6: a lollipop way revisits a node; the cut must use its later index."""
    net = Network()
    w = net.polyline(
        [(0, 0), (100, 0), (200, 0), (300, 0), (300, 100), (200, 100)], TRAIL, spacing_m=25
    )
    c = next(n for n in net.way_nodes(w) if net.node_xy(n) == (200.0, 0.0))
    xml = net.to_xml()
    data, original = read_osm_xml(xml), read_osm_xml(xml)
    for d in (data, original):
        d.set_way_nodes(d.ways[w], d.ways[w].nodes + [c])
    assert original.ways[w].nodes[-1] == c
    path = [(0, 0), (300, 0), (300, 100), (200, 100), (200, 0)]
    export = itrs_export_along(
        net, path, [_seg(450, 2, 1), _seg(150, 3, 1)], track_name="Test Trail"
    )
    matches = [match_track(parse_track(export), data, MatchOptions())]
    result = apply_matches(data, matches, CutOptions())
    check_integrity(data, original, result)
    change = next(ch for ch in result.ways if ch.way_id == w)
    assert change.status == "split"
    last = max(change.pieces, key=lambda p: p.start_m)
    assert data.ways[last.way_id].nodes[-1] == c
    assert existing_itrs(data.ways[last.way_id].tags)[TECH_KEY] == "3"


def test_no_spurious_zero_meter_extension_notes():
    net = Network()
    net.polyline([(0, 0), (400, 0)], TRAIL, spacing_m=25)
    _, _, result = run(net, [("Test Trail", [(0, 0), (400, 0)], [_seg(400, 2, 1)])])
    assert not any("extended 0 m" in n for w in result.ways for n in w.notes)


def test_junction_means_another_highway_not_any_parent_way():
    """A node shared only with a fence or area outline must not attract a cut."""
    net = Network()
    w = net.polyline([(0, 0), (400, 0)], TRAIL, spacing_m=25)
    shared = next(n for n in net.way_nodes(w) if net.node_xy(n) == (225.0, 0.0))
    net.polyline([shared, (225, 50)], {"barrier": "fence"}, spacing_m=25)
    data, original, result = run(
        net, [("Test Trail", [(0, 0), (400, 0)], [_seg(213, 2, 1), _seg(187, 3, 1)])]
    )
    check_integrity(data, original, result)
    cut = result.ways[0].cuts[0]
    assert cut.kind == "inserted", cut
