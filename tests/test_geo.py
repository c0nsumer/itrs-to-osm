from __future__ import annotations

import math

import pytest

from itrs_to_osm.geo import (
    LocalProjection,
    buffer_bbox,
    cumulative,
    merge_bboxes,
    name_similarity,
    normalize_name,
    percentile,
    point_at,
    project_point,
)


def test_local_projection_round_trips_and_scales():
    proj = LocalProjection(-87.80, 47.45)
    x, y = proj.to_xy(-87.80 + 0.001, 47.45 + 0.001)
    k = 6371000 * math.pi / 180
    assert x == pytest.approx(k * math.cos(math.radians(47.45)) * 0.001, rel=1e-6)
    assert y == pytest.approx(k * 0.001, rel=1e-6)
    lon, lat = proj.to_lonlat(x, y)
    assert lon == pytest.approx(-87.799, abs=1e-9)
    assert lat == pytest.approx(47.451, abs=1e-9)


def test_project_point_interior_and_endpoints():
    line = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    cum = cumulative(line)
    assert cum == [0.0, 100.0, 200.0]
    p = project_point((50.0, 7.0), line, cum)
    assert p.distance == pytest.approx(7.0)
    assert p.along == pytest.approx(50.0)
    assert p.index == 0 and p.t == pytest.approx(0.5)
    # Beyond the end: clamps to the last vertex.
    p = project_point((130.0, 140.0), line, cum)
    assert p.along == pytest.approx(200.0)
    assert p.point == (100.0, 100.0)
    # Before the start.
    p = project_point((-10.0, 0.0), line, cum)
    assert p.along == 0.0 and p.distance == pytest.approx(10.0)
    # A corner picks the nearer edge.
    p = project_point((103.0, 20.0), line, cum)
    assert p.index == 1 and p.along == pytest.approx(120.0)


def test_project_point_single_vertex_polyline():
    p = project_point((3.0, 4.0), [(0.0, 0.0)])
    assert p.distance == 5.0 and p.along == 0.0


def test_point_at_interpolates_and_clamps():
    line = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    cum = cumulative(line)
    xy, i, t = point_at(line, cum, 150.0)
    assert xy == (100.0, 50.0) and i == 1 and t == pytest.approx(0.5)
    assert point_at(line, cum, -5)[0] == (0.0, 0.0)
    assert point_at(line, cum, 999)[0] == (100.0, 100.0)
    assert point_at(line, cum, 100.0)[0] == (100.0, 0.0)


def test_percentile_nearest_rank():
    vals = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 90) == 5.0
    assert percentile(vals, 0) == 1.0
    assert percentile([], 90) == 0.0


def test_buffer_bbox_grows_by_meters():
    box = (-87.80, 47.45, -87.79, 47.46)
    out = buffer_bbox(box, 100.0)
    k = 6371000 * math.pi / 180
    assert out[1] == pytest.approx(47.45 - 100 / k)
    assert out[3] == pytest.approx(47.46 + 100 / k)
    assert out[0] < box[0] and out[2] > box[2]


def test_merge_bboxes_unions_overlaps_only():
    a = (0.0, 0.0, 1.0, 1.0)
    b = (0.5, 0.5, 2.0, 2.0)
    c = (10.0, 10.0, 11.0, 11.0)
    d = (1.5, 1.5, 3.0, 3.0)  # overlaps b, hence joins a via b
    merged = merge_bboxes([a, c, b, d])
    assert sorted(merged) == [(0.0, 0.0, 3.0, 3.0), c]


def test_name_normalize_and_similarity():
    assert normalize_name("  Loam   Rider!") == "loam rider"
    assert name_similarity("Loam Rider", "loam rider") == 1.0
    assert name_similarity("On the Edge", "On the Edge") == 1.0
    partial = name_similarity("KPT Fish Cove Pt. 1", "Keweenaw Point Trail (Keystone - Fish Cove)")
    assert 0.2 < partial < 0.5
    weak = name_similarity("Visp Phoenix aus der Asche CTR", "The Phoenix")
    assert 0.0 < weak < 0.3
    assert name_similarity("Loam Rider", "Thunderbolt") == 0.0
    assert name_similarity("", "x") == 0.0
