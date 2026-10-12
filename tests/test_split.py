"""Cutting, tagging, and relation fix-up over the synthetic scenarios."""

from __future__ import annotations

import pytest
from fixtures.builder import SCENARIOS

from itrs_to_osm.itrs import parse_track
from itrs_to_osm.match import MatchOptions, match_track
from itrs_to_osm.osm import read_osm_xml, to_josm_xml
from itrs_to_osm.split import CutOptions, apply_matches
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY, existing_itrs

TOL_M = 6.0
KIND = {"snap": {"node", "junction"}, "insert": {"inserted"}, "junction": {"junction"}}


def pipeline(name: str, *, force: bool = False):
    xml, export, expect = SCENARIOS[name]()
    track = parse_track(export, source=name)
    data = read_osm_xml(xml)
    original = read_osm_xml(xml)
    matches = [match_track(track, data, MatchOptions())]
    result = apply_matches(data, matches, CutOptions(force=force))
    return data, original, result, expect


def itrs_pieces(result, way_id):
    change = next(w for w in result.ways if w.way_id == way_id)
    return [(p.start_m, p.end_m, existing_itrs(p.tags_after)) for p in change.pieces]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_pieces_cuts_and_untouched_ways(name):
    data, original, result, expect = pipeline(name)
    expected_pieces = {int(k): v for k, v in expect["pieces"].items()}

    for way_id, pieces in expected_pieces.items():
        if expect["refused"]:
            continue  # covered by the refusal test
        got = itrs_pieces(result, way_id)
        # Only pieces that carry ITRS tags are listed in the ground truth.
        tagged = [g for g in got if g[2]]
        assert len(tagged) == len(pieces), (way_id, got, pieces)
        for (start, end, tags), want in zip(tagged, pieces):
            assert start == pytest.approx(want["from_m"], abs=TOL_M), (way_id, start, want)
            assert end == pytest.approx(want["to_m"], abs=TOL_M), (way_id, end, want)
            assert tags == want["tags"], (way_id, tags, want)

    for way_id, cuts in expect["cuts"].items():
        way_id = int(way_id)
        change = next((w for w in result.ways if w.way_id == way_id), None)
        got = change.cuts if change else []
        assert len(got) == len(cuts), (way_id, [(c.at_m, c.kind) for c in got], cuts)
        for cut, want in zip(sorted(got, key=lambda c: c.at_m), cuts):
            assert cut.kind in KIND[want["kind"]], (cut, want)
            assert cut.at_m == pytest.approx(want["at_m"], abs=TOL_M), (cut, want)
            if want["node_id"] is not None:
                assert cut.node_id == want["node_id"]
            else:
                assert cut.node_id < 0

    for way_id in expect["untouched_way_ids"]:
        assert data.ways[way_id].nodes == original.ways[way_id].nodes
        assert data.ways[way_id].tags == original.ways[way_id].tags
        assert data.ways[way_id].action is None

    # The JOSM writer accepts the result, and every modified element is flagged.
    xml = to_josm_xml(data, generator="test")
    assert "<osm" in xml
    for w in result.modified_ways:
        assert data.ways[w.way_id].action == "modify"

    # Split integrity: pieces are non-degenerate, consecutive pieces share their
    # boundary node, and together they reproduce the original node sequence.
    for w in result.ways:
        if w.status != "split":
            continue
        ordered = sorted(w.pieces, key=lambda p: p.start_m)
        rebuilt: list[int] = []
        for p in ordered:
            nodes = data.ways[p.way_id].nodes
            assert len(nodes) >= 2, (w.way_id, p)
            assert p.node_count == len(nodes)
            if rebuilt:
                assert rebuilt[-1] == nodes[0], (w.way_id, p)
                rebuilt.extend(nodes[1:])
            else:
                rebuilt.extend(nodes)
        inserted = {c.node_id for c in w.cuts if c.kind == "inserted"}
        assert [n for n in rebuilt if n not in inserted] == original.ways[w.way_id].nodes


def test_three_segments_merge_equal_ratings_into_one_cut():
    data, _, result, expect = pipeline("single_way_three_segments")
    change = result.ways[0]
    assert len(change.cuts) == 1 and change.cuts[0].kind == "inserted"
    assert change.status == "split"
    assert result.nodes_added == 1 and result.ways_added == 1
    # The piece with more nodes keeps the id.
    keeper = next(p for p in change.pieces if p.keeps_id)
    assert keeper.way_id == expect["ways"]["trail"]
    assert keeper.node_count >= max(p.node_count for p in change.pieces)
    new_piece = next(p for p in change.pieces if not p.keeps_id)
    assert new_piece.way_id < 0
    # New piece copies every original tag and adds the ITRS ones.
    assert data.ways[new_piece.way_id].tags["highway"] == "path"
    assert data.ways[new_piece.way_id].tags["name"] == data.ways[keeper.way_id].tags["name"]


def test_boundary_snaps_to_node_and_inserts_elsewhere():
    data, _, result, expect = pipeline("boundary_near_existing_node")
    cuts = sorted(result.ways[0].cuts, key=lambda c: c.at_m)
    assert cuts[0].kind == "node" and cuts[0].node_id == expect["snap_node_id"]
    assert 0 < cuts[0].moved_m <= 5.0
    assert cuts[1].kind == "inserted" and cuts[1].node_id < 0
    inserted = data.nodes[cuts[1].node_id]
    assert inserted.version is None and inserted.action is None
    assert result.nodes_added == 1


def test_route_relation_gets_pieces_in_order():
    data, _, result, expect = pipeline("way_in_route_relation")
    relation = data.relations[expect["relations"]["route"]]
    assert relation.action == "modify"
    change = next(w for w in result.ways if w.way_id == expect["ways"]["trail"])
    assert len(change.relations) == 1
    rc = change.relations[0]
    assert rc.direction == "forward" and rc.warning is None
    pieces = sorted(change.pieces, key=lambda p: p.start_m)
    want = expect["relation_members_after"]
    assert len(relation.members) == len(want)
    for m, w in zip(relation.members, want):
        assert m.type == "way" and m.role == ""
        if w["way_id"] is not None and w["from_m"] is None:
            assert m.ref == w["way_id"]
        else:
            piece = next(p for p in pieces if abs(p.start_m - w["from_m"]) < TOL_M)
            assert m.ref == piece.way_id
    keeper = next(p for p in change.pieces if p.keeps_id)
    assert keeper.start_m == pytest.approx(expect["keeps_id"][0], abs=TOL_M)
    assert keeper.end_m == pytest.approx(expect["keeps_id"][1], abs=TOL_M)


def test_existing_differing_value_is_refused_unless_forced():
    data, original, result, expect = pipeline("existing_itrs_tags")
    way_id = expect["ways"]["trail"]
    change = result.ways[0]
    assert change.status == "refused"
    assert [(r.key, r.old, r.new) for r in change.refusals] == [
        (r["key"], r["old"], r["new"]) for r in expect["refused"]
    ]
    assert data.ways[way_id].tags == original.ways[way_id].tags
    assert data.ways[way_id].action is None
    assert result.refusals and not result.modified_ways

    data, _, result, expect = pipeline("existing_itrs_tags", force=True)
    change = result.ways[0]
    assert change.status == "tagged" and not change.refusals
    piece = change.pieces[0]
    assert piece.changed == {TECH_KEY: ("2", "3")}
    want = (
        expect["pieces_with_force"][str(way_id)]
        if str(way_id) in expect["pieces_with_force"]
        else expect["pieces_with_force"][way_id]
    )
    assert existing_itrs(data.ways[way_id].tags) == want[0]["tags"]
    assert data.ways[way_id].action == "modify"


def test_not_rated_zero_writes_only_the_rated_key():
    data, _, result, expect = pipeline("not_rated_zero")
    way_id = expect["ways"]["trail"]
    tags = existing_itrs(data.ways[way_id].tags)
    assert TECH_KEY in tags and EXPOSURE_KEY not in tags


def test_paved_road_is_never_tagged():
    data, original, result, expect = pipeline("paved_road_segment")
    road = expect["ways"]["road"]
    assert data.ways[road].tags == original.ways[road].tags
    assert all(w.way_id != road for w in result.ways)
    for way_id in expect["trail_way_ids"]:
        assert existing_itrs(data.ways[way_id].tags)


def test_already_tagged_way_is_left_unchanged():
    xml, export, expect = SCENARIOS["single_way_uniform"]()
    data = read_osm_xml(xml)
    way = data.ways[expect["ways"]["trail"]]
    way.tags[TECH_KEY] = "2"
    way.tags[EXPOSURE_KEY] = "1"
    track = parse_track(export)
    result = apply_matches(data, [match_track(track, data)])
    change = result.ways[0]
    assert change.status == "unchanged"
    assert way.action is None
    assert any("already tagged" in n for n in change.notes)


def test_two_tracks_sharing_a_way_resolve_by_longer_claim():
    xml, export, expect = SCENARIOS["single_way_three_segments"]()
    data = read_osm_xml(xml)
    track_a = parse_track(export, source="a")
    # A second track: same geometry, every segment rated (4,1), named differently.
    export_b = SCENARIOS["single_way_three_segments"]()[1]
    for f in export_b["features"]:
        p = f["properties"]
        if f["geometry"]["type"] == "LineString":
            p["segment_tech_rating"] = 4
            p["segment_track_name"] = "Other"
        elif p.get("point_type") == "Start":
            p["point_start_track_name"] = "Other"
    track_b = parse_track(export_b, source="b")
    matches = [match_track(track_a, data), match_track(track_b, data)]
    result = apply_matches(data, matches)
    change = result.ways[0]
    # Track B's claim is one 600 m zone; it beats A's 400 m and 200 m zones.
    assert change.notes and any("longer claim wins" in n for n in change.notes)
    assert len(change.cuts) == 0
    assert existing_itrs(data.ways[expect["ways"]["trail"]].tags) == {
        TECH_KEY: "4",
        EXPOSURE_KEY: "1",
    }
