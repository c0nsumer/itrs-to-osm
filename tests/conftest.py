"""Shared fixtures: build ITRS-shaped GeoJSON in memory without any real data."""

from __future__ import annotations

import copy
from typing import Any

import pytest

# A short synthetic trail: a gentle curve, ~8 m vertex spacing, near 47.4N.
BASE_LON, BASE_LAT = -87.80, 47.45
DLON = 0.0001  # ~7.5 m at this latitude
DLAT = 0.00003


def positions(n: int, start: int = 0) -> list[list[float]]:
    return [
        [BASE_LON + (start + i) * DLON, BASE_LAT + (start + i) * DLAT, 300.0 - i] for i in range(n)
    ]


def segment_feature(
    name: str,
    position: int,
    coords: list[list[float]],
    tech: Any = 2,
    exposure: Any = 1,
    *,
    style: str = "5.x",
    track_name: str = "Test Trail",
    track_uuid: str = "-TRACK",
    track_type: str = "Single trail",
    use: str = "Shared use",
    direction: str = "Bi-directional",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "segment_uuid": f"-SEG{position:03d}",
        "segment_name": name,
        "segment_itrs_version": "5.3" if style == "5.x" else "4.4",
        "segment_track_type": track_type,
        "segment_length_m": int(7.6 * (len(coords) - 1)),
        "segment_use": use,
        "segment_direction": direction,
        "segment_tech_rating": tech,
        "segment_max_exposure": exposure,
        "segment_position_in_trail": position,
        "segment_last_mutated_on_utc": "2026-06-24T13:30:44.037749Z",
    }
    if style == "5.x":
        props["segment_track_name"] = track_name
        props["segment_track_uuid"] = track_uuid
    else:
        props["segment_trail_name"] = track_name
        props["segment_trail_uuid"] = track_uuid
    if extra:
        props.update(extra)
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def start_feature(coords: list[float], **overrides: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "point_type": "Start",
        "point_start_track_name": "Test Trail",
        "point_start_track_uuid": "-TRACK",
        "point_start_track_length_m": 300,
        "point_start_track_max_tech_rating": 3,
        "point_start_track_max_exposure_rating": 1,
        "point_start_track_share_techLevel_0_perc": 0.0,
    }
    props.update(overrides)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coords},
        "properties": props,
    }


def point_feature(
    point_type: str, coords: list[float], segment_uuid: str, **props: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "point_type": point_type,
        "point_segment_uuid": segment_uuid,
        "point_segment_name": "S001",
        "point_track_uuid": "-TRACK",
        "point_track_name": "Test Trail",
        "point_remark": "",
        "point_photo_1": "",
        "point_photo_2": "",
        "point_photo_3": "",
    }
    base.update(props)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coords},
        "properties": base,
    }


def make_export(
    segments: list[dict[str, Any]],
    points: list[dict[str, Any]] | None = None,
    *,
    style: str = "5.x",
) -> dict[str, Any]:
    first = segments[0]["geometry"]["coordinates"][0]
    features = [start_feature(first)] + segments + (points or [])
    data: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if style == "5.x":
        data = {
            "type": "FeatureCollection",
            "version": "1.0",
            "date": "2026-02-04",
            "features": features,
        }
    return copy.deepcopy(data)


@pytest.fixture
def three_segment_export() -> dict[str, Any]:
    """S001 (tech 2) -> S002 (tech 2) -> S003 (tech 3), contiguous."""
    a = positions(10, 0)
    b = positions(10, 9)
    c = positions(10, 18)
    return make_export(
        [
            segment_feature("S001", 1, a, tech=2, exposure=1),
            segment_feature("S002", 2, b, tech=2, exposure=1),
            segment_feature("S003", 3, c, tech=3, exposure=1),
        ]
    )
