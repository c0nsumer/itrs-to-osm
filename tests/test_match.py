"""Matcher tests over the synthetic scenarios' ground truth."""

from __future__ import annotations

import pytest
from fixtures.builder import SCENARIOS

from itrs_to_osm.itrs import parse_track
from itrs_to_osm.match import MatchOptions, class_penalty, match_track
from itrs_to_osm.osm import read_osm_xml

# Chainage in the matcher's local frame vs the builder's haversine meters, plus
# a vertex's worth of slack where a boundary falls between samples.
TOL_M = 6.0


def run(name: str, **opts):
    xml, export, expect = SCENARIOS[name]()
    track = parse_track(export, source=name)
    data = read_osm_xml(xml)
    return match_track(track, data, MatchOptions(**opts)), expect


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_ground_truth(name):
    result, expect = run(name)
    # matched_way_ids, when present, is per segment and may include ways that
    # get matched but never tagged (a paved road); trail_way_ids are the rest.
    per_segment = expect.get("matched_way_ids")
    unmatched = set(expect["unmatched_segments"])

    for sm in result.segments:
        if sm.segment.name in unmatched:
            assert sm.status == "unmatched", (sm.segment.name, sm.status, sm.reasons)
            assert not sm.intervals
            continue
        assert sm.status in ("matched", "partial"), (sm.segment.name, sm.status, sm.reasons)
        if isinstance(per_segment, dict):
            allowed = set(per_segment[sm.segment.name])
        else:
            allowed = set(expect["trail_way_ids"])
        for iv in sm.intervals:
            assert iv.way_id in allowed, (sm.segment.name, iv)
            assert iv.p90_offset <= 15.0

    # Every expected dropped tail shows up as dropped, with roughly its length.
    dropped = [(d.way_id, d.length) for sm in result.segments for d in sm.dropped]
    for tail in expect["dropped_tails"]:
        assert any(
            w == tail["way_id"] and abs(length - tail["length_m"]) < 10.0 for w, length in dropped
        ), (tail, dropped)
    if not expect["dropped_tails"]:
        assert dropped == []

    # Coverage: per trail way, the intervals span the expected extent.
    by_way = result.intervals_by_way()
    for way_id in expect["trail_way_ids"]:
        pieces = expect["pieces"].get(str(way_id)) or expect["pieces"].get(way_id)
        if not pieces or way_id not in by_way:
            continue
        lo = min(p["from_m"] for p in pieces)
        hi = max(p["to_m"] for p in pieces)
        starts = [iv.start for _, iv in by_way[way_id]]
        ends = [iv.end for _, iv in by_way[way_id]]
        assert min(starts) == pytest.approx(lo, abs=TOL_M), (way_id, min(starts), lo)
        assert max(ends) == pytest.approx(hi, abs=TOL_M), (way_id, max(ends), hi)

    # Every expected cut sits on an interval boundary.
    for way_id, cuts in expect["cuts"].items():
        way_id = int(way_id)
        if way_id not in by_way:
            assert not cuts
            continue
        boundaries = [iv.start for _, iv in by_way[way_id]] + [iv.end for _, iv in by_way[way_id]]
        for cut in cuts:
            assert any(abs(b - cut["at_m"]) < TOL_M for b in boundaries), (way_id, cut, boundaries)


def test_junction_scenario_prefers_connected_switch_over_parallel():
    result, expect = run("junction_with_parallel_trail")
    main = expect["ways"]["main"] if "main" in expect["ways"] else expect["trail_way_ids"][0]
    for sm in result.segments:
        assert sm.way_ids == [main], (sm.segment.name, sm.way_ids)


def test_track_spans_two_ways_meets_at_junction():
    result, expect = run("track_spans_two_ways")
    first, second = expect["ways"]["first"], expect["ways"]["second"]
    s1, s2 = result.segments
    assert s1.way_ids == [first] and s2.way_ids == [second]
    # The first interval runs to the end of the first way; the second starts at 0.
    assert s1.intervals[-1].end == pytest.approx(300.0, abs=TOL_M)
    assert s2.intervals[0].start == pytest.approx(0.0, abs=TOL_M)
    assert s2.intervals[0].start_pos == (0, 0.0)


def test_name_prior_reported_from_relation():
    result, expect = run("way_in_route_relation")
    trail = expect["ways"]["trail"]
    assert trail in result.name_matches
    score, where = result.name_matches[trail]
    assert score == 1.0 and where.startswith("relation ")


def test_way_id_restriction_and_name_hint():
    xml, export, expect = SCENARIOS["track_spans_two_ways"]()
    track = parse_track(export)
    data = read_osm_xml(xml)
    first = expect["ways"]["first"]
    result = match_track(track, data, MatchOptions(way_ids={first}))
    assert all(iv.way_id == first for sm in result.segments for iv in sm.intervals)
    assert result.candidate_count == 1
    # A name hint that matches nothing yields no name prior at all.
    result = match_track(track, data, MatchOptions(name_hint="Nope"))
    assert result.name_matches == {}


def test_reversed_track_is_flagged():
    xml, export, expect = SCENARIOS["single_way_uniform"]()
    # Reverse the track: walk the way from its end back to its start.
    for f in export["features"]:
        if f["geometry"]["type"] == "LineString":
            f["geometry"]["coordinates"].reverse()
    start = [f for f in export["features"] if f["properties"].get("point_type") == "Start"][0]
    seg = [f for f in export["features"] if f["geometry"]["type"] == "LineString"][0]
    start["geometry"]["coordinates"] = seg["geometry"]["coordinates"][0]
    result = match_track(parse_track(export), read_osm_xml(xml))
    iv = result.segments[0].intervals[0]
    assert iv.reversed
    assert iv.start == pytest.approx(0.0, abs=TOL_M)


def test_no_candidates_reports_unmatched():
    xml, export, _ = SCENARIOS["single_way_uniform"]()
    data = read_osm_xml(xml)
    for way in data.ways.values():
        way.tags.pop("highway", None)
    result = match_track(parse_track(export), data)
    assert result.candidate_count == 0
    assert result.segments[0].status == "unmatched"
    assert any("no candidate" in w for w in result.warnings)


def test_class_penalty_table():
    assert class_penalty("Single trail", "path") == 0.0
    assert class_penalty("Single trail", "track") == 1.0
    assert class_penalty("Single trail", "residential") == 2.0
    assert class_penalty("Unsurfaced road/double track", "track") == 0.0
    assert class_penalty("Paved/surfaced road", "residential") == 0.0
    assert class_penalty("Paved/surfaced road", "path") == 2.0
    assert class_penalty("", "path") == 0.0
