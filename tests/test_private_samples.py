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
