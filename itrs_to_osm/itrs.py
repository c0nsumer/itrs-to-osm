"""Parse ITRS GeoJSON track exports into plain Python objects.

An export is a FeatureCollection holding one ``Start`` point (track-level
summary), one LineString per rated segment, and optional rating points
(Exposure, GeneralAssessmentRemark, Water, RockfallZone, RiverCrossing,
Wildlife, ...) that reference a segment by uuid.

Field names vary between ITRS versions. Every name the parser knows about is
listed in the alias tables below, newest first, so a rename in a future
export is a one-line change here and nowhere else.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Rating value meanings (ITRS rating data explanation, p. 9): 0 = Not rated,
# 1 = Green, 2 = Blue, 3 = Red, 4 = Black, 5 = Orange. Technical difficulty
# runs 1-5, exposure 1-4. 0 appears in exports for anything not assessed.
NOT_RATED = 0
TECH_RATINGS = range(0, 6)
EXPOSURE_RATINGS = range(0, 5)

TRACK_TYPE_PAVED = "Paved/surfaced road"
TRACK_TYPE_UNSURFACED = "Unsurfaced road/double track"
TRACK_TYPE_SINGLE = "Single trail"

USE_SHARED = "Shared use"
USE_BIKE_ONLY = "Bike-only"
DIRECTION_ONE_WAY = "Uni-directional"
DIRECTION_TWO_WAY = "Bi-directional"

# Segments are contiguous in every export seen; a gap bigger than this
# between one segment's end and the next one's start is reported.
CONTIGUITY_WARN_M = 2.0

# Canonical attribute -> property names seen in exports, newest first.
SEGMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("segment_name",),
    "uuid": ("segment_uuid",),
    "position": ("segment_position_in_trail",),
    "track_name": ("segment_track_name", "segment_trail_name"),
    "track_uuid": ("segment_track_uuid", "segment_trail_uuid"),
    "tech_rating": ("segment_tech_rating",),
    "max_exposure": ("segment_max_exposure",),
    "direction": ("segment_direction",),
    "use": ("segment_use",),
    "track_type": ("segment_track_type",),
    "length_m": ("segment_length_m",),
    "itrs_version": ("segment_itrs_version",),
    "last_mutated": ("segment_last_mutated_on_utc",),
}

START_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("point_start_track_name",),
    "uuid": ("point_start_track_uuid",),
    "length_m": ("point_start_track_length_m",),
    "max_tech_rating": ("point_start_track_max_tech_rating",),
    "max_exposure_rating": ("point_start_track_max_exposure_rating",),
}

POINT_ALIASES: dict[str, tuple[str, ...]] = {
    "segment_uuid": ("point_segment_uuid",),
    "segment_name": ("point_segment_name",),
    "track_name": ("point_track_name",),
    "track_uuid": ("point_track_uuid",),
    "remark": ("point_remark",),
}

POINT_PHOTO_KEYS = ("point_photo_1", "point_photo_2", "point_photo_3")


class ItrsError(ValueError):
    """The file is not an ITRS track export the tool can use."""


def _first(props: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in props:
            return props[name]
    return None


def _as_int(value: Any, what: str) -> int:
    """Coerce an int-like value (int, integral float, numeric string)."""
    if isinstance(value, bool):
        raise ItrsError(f"{what}: expected a number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as e:
            raise ItrsError(f"{what}: expected a number, got {value!r}") from e
        if number.is_integer():
            return int(number)
    raise ItrsError(f"{what}: expected a whole number, got {value!r}")


def _as_float(value: Any) -> float | None:
    """Coerce a float-like value, tolerating strings; None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _as_name(value: Any) -> str:
    """A name as typed in the app, minus surrounding whitespace ("Cabin Life ")."""
    return _as_str(value).strip()


def _coords_3d(raw: Any, what: str) -> list[tuple[float, float, float | None]]:
    """Normalize GeoJSON positions to (lon, lat, elevation-or-None)."""
    if not isinstance(raw, list) or not raw:
        raise ItrsError(f"{what}: missing coordinates")
    out: list[tuple[float, float, float | None]] = []
    for pos in raw:
        if not isinstance(pos, list) or len(pos) < 2:
            raise ItrsError(f"{what}: malformed position {pos!r}")
        lon, lat = float(pos[0]), float(pos[1])
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ItrsError(f"{what}: position out of range {pos!r}")
        ele = float(pos[2]) if len(pos) > 2 and pos[2] is not None else None
        out.append((lon, lat, ele))
    return out


def distance_m(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Haversine distance in meters between two (lon, lat, ...) positions."""
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Segment:
    """One rated trail segment (a LineString feature)."""

    name: str
    uuid: str
    position: int
    coords: list[tuple[float, float, float | None]]
    tech_rating: int
    max_exposure: int
    direction: str
    use: str
    track_type: str
    length_m: float | None
    itrs_version: str
    last_mutated: str
    track_name: str
    track_uuid: str
    props: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_paved_road(self) -> bool:
        return self.track_type == TRACK_TYPE_PAVED

    @property
    def is_bike_only(self) -> bool:
        return self.use == USE_BIKE_ONLY

    @property
    def is_one_way(self) -> bool:
        return self.direction == DIRECTION_ONE_WAY

    @property
    def tech_rated(self) -> bool:
        return self.tech_rating != NOT_RATED

    @property
    def exposure_rated(self) -> bool:
        return self.max_exposure != NOT_RATED

    def geometry_length_m(self) -> float:
        return sum(distance_m(a, b) for a, b in zip(self.coords, self.coords[1:]))


@dataclass
class RatingPoint:
    """A point feature other than Start: Exposure, GeneralAssessmentRemark, ..."""

    point_type: str
    lon: float
    lat: float
    ele: float | None
    segment_uuid: str
    segment_name: str
    remark: str
    photos: list[str]
    props: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def value(self) -> Any:
        """The point's main value, whatever its type.

        Exposure points carry ``point_exposure_rating``; every other type
        seen in ITRS's own QGIS styles carries ``point_<type>_value`` with
        the type's first letter lowercased (``point_riverCrossing_value``).
        """
        if self.point_type == "Exposure":
            return self.props.get("point_exposure_rating")
        if not self.point_type:
            return None
        key = f"point_{self.point_type[0].lower()}{self.point_type[1:]}_value"
        return self.props.get(key)


@dataclass
class Track:
    """One ITRS track export: the whole file."""

    name: str
    uuid: str
    source: str
    file_version: str
    export_date: str
    length_m: float | None
    max_tech_rating: int | None
    max_exposure_rating: int | None
    segments: list[Segment]
    points: list[RatingPoint]
    start: tuple[float, float, float | None] | None
    start_props: dict[str, Any] = field(default_factory=dict, repr=False)
    warnings: list[str] = field(default_factory=list)

    @property
    def itrs_versions(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.itrs_version and seg.itrs_version not in seen:
                seen.append(seg.itrs_version)
        return seen

    @property
    def survey_dates(self) -> list[str]:
        """Distinct ``segment_last_mutated_on_utc`` dates (YYYY-MM-DD), sorted."""
        return sorted({seg.last_mutated[:10] for seg in self.segments if seg.last_mutated})

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat) over all segment vertices."""
        lons = [c[0] for seg in self.segments for c in seg.coords]
        lats = [c[1] for seg in self.segments for c in seg.coords]
        return (min(lons), min(lats), max(lons), max(lats))

    def segment_by_uuid(self, uuid: str) -> Segment | None:
        for seg in self.segments:
            if seg.uuid == uuid:
                return seg
        return None

    def points_for(self, segment: Segment) -> list[RatingPoint]:
        return [p for p in self.points if p.segment_uuid == segment.uuid]


def _parse_segment(feature: dict[str, Any], index: int) -> Segment:
    props = feature.get("properties") or {}
    what = f"segment feature #{index}"
    name = _as_name(_first(props, SEGMENT_ALIASES["name"])) or f"#{index}"
    what = f"segment {name}"
    coords = _coords_3d((feature.get("geometry") or {}).get("coordinates"), what)
    if len(coords) < 2:
        raise ItrsError(f"{what}: a segment needs at least two positions")
    position_raw = _first(props, SEGMENT_ALIASES["position"])
    position = _as_int(position_raw, f"{what} position") if position_raw is not None else index
    tech_raw = _first(props, SEGMENT_ALIASES["tech_rating"])
    exposure_raw = _first(props, SEGMENT_ALIASES["max_exposure"])
    if tech_raw is None:
        raise ItrsError(f"{what}: missing {SEGMENT_ALIASES['tech_rating'][0]}")
    if exposure_raw is None:
        raise ItrsError(f"{what}: missing {SEGMENT_ALIASES['max_exposure'][0]}")
    return Segment(
        name=name,
        uuid=_as_str(_first(props, SEGMENT_ALIASES["uuid"])),
        position=position,
        coords=coords,
        tech_rating=_as_int(tech_raw, f"{what} technical rating"),
        max_exposure=_as_int(exposure_raw, f"{what} exposure"),
        direction=_as_str(_first(props, SEGMENT_ALIASES["direction"])),
        use=_as_str(_first(props, SEGMENT_ALIASES["use"])),
        track_type=_as_str(_first(props, SEGMENT_ALIASES["track_type"])),
        length_m=_as_float(_first(props, SEGMENT_ALIASES["length_m"])),
        itrs_version=_as_str(_first(props, SEGMENT_ALIASES["itrs_version"])),
        last_mutated=_as_str(_first(props, SEGMENT_ALIASES["last_mutated"])),
        track_name=_as_name(_first(props, SEGMENT_ALIASES["track_name"])),
        track_uuid=_as_str(_first(props, SEGMENT_ALIASES["track_uuid"])),
        props=props,
    )


def _parse_point(feature: dict[str, Any], index: int) -> RatingPoint:
    props = feature.get("properties") or {}
    point_type = _as_str(props.get("point_type"))
    coords = _coords_3d([(feature.get("geometry") or {}).get("coordinates")], f"point #{index}")
    lon, lat, ele = coords[0]
    photos = [_as_str(props.get(k)) for k in POINT_PHOTO_KEYS if _as_str(props.get(k))]
    return RatingPoint(
        point_type=point_type,
        lon=lon,
        lat=lat,
        ele=ele,
        segment_uuid=_as_str(_first(props, POINT_ALIASES["segment_uuid"])),
        segment_name=_as_name(_first(props, POINT_ALIASES["segment_name"])),
        remark=_as_str(_first(props, POINT_ALIASES["remark"])).strip(),
        photos=photos,
        props=props,
    )


def parse_track(data: dict[str, Any], source: str = "<memory>") -> Track:
    """Build a Track from a decoded GeoJSON object.

    Raises ItrsError for anything structurally unusable. Oddities that the
    rest of the pipeline can live with (a gap between segments, a rating
    outside the documented range, a point that references no segment) are
    appended to ``Track.warnings`` instead.
    """
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ItrsError(f"{source}: not a GeoJSON FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise ItrsError(f"{source}: no features")

    warnings: list[str] = []
    starts: list[dict[str, Any]] = []
    segments: list[Segment] = []
    points: list[RatingPoint] = []

    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            raise ItrsError(f"{source}: feature #{index} is not an object")
        geometry = feature.get("geometry") or {}
        gtype = geometry.get("type")
        props = feature.get("properties") or {}
        if gtype == "LineString":
            segments.append(_parse_segment(feature, index))
        elif gtype == "Point":
            if props.get("point_type") == "Start":
                starts.append(feature)
            else:
                points.append(_parse_point(feature, index))
        else:
            warnings.append(f"feature #{index}: ignored geometry type {gtype!r}")

    if not starts:
        raise ItrsError(f"{source}: no Start point (point_type = 'Start')")
    if len(starts) > 1:
        raise ItrsError(f"{source}: {len(starts)} Start points; expected exactly one")
    if not segments:
        raise ItrsError(f"{source}: no segments (LineString features)")

    start_feature = starts[0]
    start_props = start_feature.get("properties") or {}
    start_coords = _coords_3d([start_feature["geometry"].get("coordinates")], "Start point")[0]

    segments.sort(key=lambda s: s.position)
    positions = [s.position for s in segments]
    if len(set(positions)) != len(positions):
        warnings.append("duplicate segment_position_in_trail values; file order used as tiebreak")

    for seg in segments:
        if seg.tech_rating not in TECH_RATINGS:
            warnings.append(f"{seg.name}: technical rating {seg.tech_rating} outside 0-5")
        if seg.max_exposure not in EXPOSURE_RATINGS:
            warnings.append(f"{seg.name}: exposure {seg.max_exposure} outside 0-4")
        if seg.track_type not in (TRACK_TYPE_PAVED, TRACK_TYPE_UNSURFACED, TRACK_TYPE_SINGLE):
            warnings.append(f"{seg.name}: unknown track type {seg.track_type!r}")
    for prev, cur in zip(segments, segments[1:]):
        gap = distance_m(prev.coords[-1], cur.coords[0])
        if gap > CONTIGUITY_WARN_M:
            warnings.append(f"{cur.name} starts {gap:.1f} m from the end of {prev.name}")

    uuids = {s.uuid for s in segments if s.uuid}
    for p in points:
        if p.segment_uuid and p.segment_uuid not in uuids:
            warnings.append(
                f"{p.point_type or 'point'} references unknown segment {p.segment_uuid}"
            )

    name = _as_name(_first(start_props, START_ALIASES["name"])) or segments[0].track_name
    uuid = _as_str(_first(start_props, START_ALIASES["uuid"])) or segments[0].track_uuid
    max_tech_raw = _first(start_props, START_ALIASES["max_tech_rating"])
    max_exp_raw = _first(start_props, START_ALIASES["max_exposure_rating"])

    return Track(
        name=name,
        uuid=uuid,
        source=source,
        file_version=_as_str(data.get("version")),
        export_date=_as_str(data.get("date")),
        length_m=_as_float(_first(start_props, START_ALIASES["length_m"])),
        max_tech_rating=_as_int(max_tech_raw, "track max tech rating")
        if max_tech_raw is not None
        else None,
        max_exposure_rating=_as_int(max_exp_raw, "track max exposure")
        if max_exp_raw is not None
        else None,
        segments=segments,
        points=points,
        start=start_coords,
        start_props=start_props,
        warnings=warnings,
    )


REEXPORT_TOL_M = 0.5


def is_reexport(a: Track, b: Track) -> bool:
    """True when ``b`` carries the same survey as ``a`` under another track uuid.

    The ITRS app can export one survey twice ("Red Trail" and "Red Trail Sync
    #2" at Copper Harbor): every segment has the same name, ratings, type,
    use, direction, survey date, and vertices, and only the uuids differ.
    Processing both is harmless (the claims coincide), but the report doubles
    up, so the caller warns.
    """
    if len(a.segments) != len(b.segments):
        return False
    for x, y in zip(a.segments, b.segments):
        if (
            x.name != y.name
            or x.tech_rating != y.tech_rating
            or x.max_exposure != y.max_exposure
            or x.track_type != y.track_type
            or x.use != y.use
            or x.direction != y.direction
            or x.last_mutated != y.last_mutated
            or len(x.coords) != len(y.coords)
        ):
            return False
        if any(distance_m(p, q) > REEXPORT_TOL_M for p, q in zip(x.coords, y.coords)):
            return False
    return True


def flag_reexports(tracks: list[Track]) -> None:
    """Warn on every track that re-exports an earlier one in the list (in place)."""
    for i, later in enumerate(tracks):
        for earlier in tracks[:i]:
            if is_reexport(earlier, later):
                later.warnings.append(
                    f"appears to be a re-export of {earlier.name!r} (same segments, ratings, "
                    "survey dates, and vertices; only the uuids differ); its results coincide "
                    "with that track's, so one of the two files can be left out"
                )
                break


def load_track(path: str | Path) -> Track:
    """Read and parse an ITRS GeoJSON export from disk."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ItrsError(f"{path}: not valid JSON ({e})") from e
    except OSError as e:
        raise ItrsError(f"{path}: {e.strerror}") from e
    return parse_track(data, source=str(path))
