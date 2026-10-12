"""Checks against the real ITRS exports, which live outside the repo.

The files are gitignored private reference material. When none are present
(a fresh public checkout), every test here is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from itrs_to_osm.itrs import EXPOSURE_RATINGS, TECH_RATINGS, load_track

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = sorted(ROOT.glob("geojson-export_*.geojson")) + sorted(
    (ROOT / "tests" / "private").glob("*.geojson")
)

pytestmark = pytest.mark.skipif(not SAMPLES, reason="no private ITRS samples present")


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem[:40])
def test_real_export_parses_cleanly(path):
    track = load_track(path)
    assert track.name
    assert track.uuid
    assert track.segments
    assert track.itrs_versions
    for seg in track.segments:
        assert seg.tech_rating in TECH_RATINGS
        assert seg.max_exposure in EXPOSURE_RATINGS
        assert seg.track_name == track.name
        assert seg.track_uuid == track.uuid
        assert len(seg.coords) >= 2
        # Declared length agrees with the geometry to within a couple of meters.
        if seg.length_m:
            assert abs(seg.geometry_length_m() - seg.length_m) < 3.0
    # Real exports are contiguous, in order, and every point references a segment.
    assert [s.position for s in track.segments] == list(range(1, len(track.segments) + 1))
    assert not [w for w in track.warnings if "starts" in w or "unknown segment" in w], (
        track.warnings
    )
    for p in track.points:
        assert track.segment_by_uuid(p.segment_uuid) is not None


def _cached_area(track):
    """The cached Overpass download for a track's area, or None (never hits the network)."""
    from itrs_to_osm.geo import buffer_bbox
    from itrs_to_osm.overpass import build_query, cache_key

    cache = ROOT / "cache"
    path = cache / f"{cache_key(build_query(buffer_bbox(track.bbox(), 100.0)))}.osm"
    return path.read_text(encoding="utf-8") if path.exists() else None


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem[:40])
def test_real_export_matches_its_osm_ways(path):
    from itrs_to_osm.match import match_track
    from itrs_to_osm.osm import read_osm_xml

    track = load_track(path)
    xml = _cached_area(track)
    if xml is None:
        pytest.skip("no cached Overpass download for this track")
    result = match_track(track, read_osm_xml(xml))
    assert result.candidate_count > 0
    # Every real sample lies on mapped trails: nothing is unmatched, and the
    # name prior found the trail (exact for two, token overlap for the rest).
    assert all(sm.status in ("matched", "partial") for sm in result.segments), [
        (sm.segment.name, sm.status, sm.reasons) for sm in result.segments
    ]
    assert result.name_matches
    # Contiguity survives matching: each segment's interval meets the next.
    for a, b in zip(result.segments, result.segments[1:]):
        last, first = a.intervals[-1], b.intervals[0]
        if last.way_id == first.way_id:
            joint = last.start if last.reversed else last.end
            other = first.end if first.reversed else first.start
            assert abs(joint - other) < 1.0, (a.segment.name, b.segment.name, joint, other)
