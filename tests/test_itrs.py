from __future__ import annotations

import json

import pytest
from conftest import make_export, point_feature, positions, segment_feature

from itrs_to_osm.itrs import (
    NOT_RATED,
    TRACK_TYPE_PAVED,
    ItrsError,
    load_track,
    parse_track,
)


def test_parses_5x_export(three_segment_export):
    track = parse_track(three_segment_export, source="t.geojson")
    assert track.name == "Test Trail"
    assert track.uuid == "-TRACK"
    assert track.file_version == "1.0"
    assert track.export_date == "2026-02-04"
    assert track.itrs_versions == ["5.3"]
    assert track.survey_dates == ["2026-06-24"]
    assert [s.name for s in track.segments] == ["S001", "S002", "S003"]
    assert [s.tech_rating for s in track.segments] == [2, 2, 3]
    assert track.segments[0].track_name == "Test Trail"
    assert track.segments[0].coords[0][2] == 300.0
    assert track.start == tuple(track.segments[0].coords[0])
    assert track.warnings == []


def test_parses_44_export_with_trail_aliases():
    segs = [segment_feature("S001", 1, positions(5), style="4.4")]
    data = make_export(segs, style="4.4")
    assert "version" not in data
    track = parse_track(data)
    assert track.file_version == ""
    assert track.export_date == ""
    assert track.itrs_versions == ["4.4"]
    assert track.segments[0].track_name == "Test Trail"
    assert track.segments[0].track_uuid == "-TRACK"


def test_segments_sorted_by_position_not_file_order():
    a, b = positions(5, 0), positions(5, 4)
    data = make_export(
        [segment_feature("S002", 2, b), segment_feature("S001", 1, a)],
    )
    track = parse_track(data)
    assert [s.name for s in track.segments] == ["S001", "S002"]
    assert track.warnings == []


def test_zero_means_not_rated_is_preserved():
    data = make_export([segment_feature("S001", 1, positions(5), tech=3, exposure=0)])
    seg = parse_track(data).segments[0]
    assert seg.max_exposure == NOT_RATED
    assert seg.tech_rated and not seg.exposure_rated


def test_numeric_strings_and_integral_floats_are_coerced():
    data = make_export(
        [
            segment_feature(
                "S001", 1, positions(5), tech="3", exposure=2.0, extra={"segment_length_m": "31"}
            )
        ]
    )
    seg = parse_track(data).segments[0]
    assert seg.tech_rating == 3
    assert seg.max_exposure == 2
    assert seg.length_m == 31.0


@pytest.mark.parametrize("bad", ["x", 2.5, None, True])
def test_non_integer_rating_is_an_error(bad):
    data = make_export([segment_feature("S001", 1, positions(5), tech=bad)])
    with pytest.raises(ItrsError):
        parse_track(data)


def test_out_of_range_rating_is_a_warning_not_an_error():
    data = make_export([segment_feature("S001", 1, positions(5), tech=7, exposure=5)])
    track = parse_track(data)
    assert track.segments[0].tech_rating == 7
    assert any("outside 0-5" in w for w in track.warnings)
    assert any("outside 0-4" in w for w in track.warnings)


def test_missing_start_point_is_an_error():
    data = make_export([segment_feature("S001", 1, positions(5))])
    data["features"] = [f for f in data["features"] if f["properties"].get("point_type") != "Start"]
    with pytest.raises(ItrsError, match="Start"):
        parse_track(data)


def test_two_start_points_is_an_error():
    data = make_export([segment_feature("S001", 1, positions(5))])
    data["features"].insert(0, data["features"][0])
    with pytest.raises(ItrsError, match="2 Start points"):
        parse_track(data)


def test_no_segments_is_an_error():
    data = make_export([segment_feature("S001", 1, positions(5))])
    data["features"] = [f for f in data["features"] if f["geometry"]["type"] != "LineString"]
    with pytest.raises(ItrsError, match="no segments"):
        parse_track(data)


def test_not_a_feature_collection():
    with pytest.raises(ItrsError):
        parse_track({"type": "Feature"})


def test_gap_between_segments_is_a_warning():
    a = positions(5, 0)
    b = positions(5, 20)  # starts ~100 m away
    data = make_export([segment_feature("S001", 1, a), segment_feature("S002", 2, b)])
    track = parse_track(data)
    assert any("S002 starts" in w and "from the end of S001" in w for w in track.warnings)


def test_rating_points_attach_to_segments_and_expose_values():
    seg = segment_feature("S001", 1, positions(6))
    c = seg["geometry"]["coordinates"][2]
    pts = [
        point_feature(
            "Exposure",
            c,
            "-SEG001",
            point_exposure_rating=3,
            point_exposure_length="up to 30m long",
        ),
        point_feature(
            "GeneralAssessmentRemark",
            c,
            "-SEG001",
            point_generalAssessmentRemark_value="View point",
            point_remark="mine pit here",
            point_photo_1="https://example/p.jpg",
        ),
        point_feature(
            "RiverCrossing",
            c,
            "-SEG001",
            point_riverCrossing_value="Crossing of small seasonal river",
        ),
        point_feature("SomethingNew", c, "-SEG001", point_somethingNew_value="future"),
    ]
    track = parse_track(make_export([seg], pts))
    assert track.warnings == []
    got = track.points_for(track.segments[0])
    assert [p.point_type for p in got] == [
        "Exposure",
        "GeneralAssessmentRemark",
        "RiverCrossing",
        "SomethingNew",
    ]
    assert [p.value for p in got] == [3, "View point", "Crossing of small seasonal river", "future"]
    assert got[1].remark == "mine pit here"
    assert got[1].photos == ["https://example/p.jpg"]
    assert got[0].ele is not None


def test_point_referencing_unknown_segment_is_a_warning():
    seg = segment_feature("S001", 1, positions(6))
    pt = point_feature(
        "Exposure", seg["geometry"]["coordinates"][1], "-NOPE", point_exposure_rating=2
    )
    track = parse_track(make_export([seg], [pt]))
    assert any("unknown segment -NOPE" in w for w in track.warnings)


def test_2d_coordinates_are_accepted():
    coords = [[p[0], p[1]] for p in positions(4)]
    data = make_export([segment_feature("S001", 1, coords)])
    seg = parse_track(data).segments[0]
    assert seg.coords[0][2] is None
    assert seg.geometry_length_m() > 20


def test_paved_road_flag_and_bbox():
    a = positions(5, 0)
    data = make_export([segment_feature("S001", 1, a, track_type=TRACK_TYPE_PAVED)])
    track = parse_track(data)
    assert track.segments[0].is_paved_road
    min_lon, min_lat, max_lon, max_lat = track.bbox()
    assert min_lon == a[0][0] and max_lon == a[-1][0]
    assert min_lat == a[0][1] and max_lat == a[-1][1]


def test_load_track_reads_file_and_reports_bad_json(tmp_path, three_segment_export):
    path = tmp_path / "x.geojson"
    path.write_text(json.dumps(three_segment_export), encoding="utf-8")
    assert load_track(path).source == str(path)
    bad = tmp_path / "bad.geojson"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ItrsError, match="not valid JSON"):
        load_track(bad)
    with pytest.raises(ItrsError):
        load_track(tmp_path / "missing.geojson")
