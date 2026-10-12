"""The ITRS -> OSM tag mapping, and what each value means.

Only two keys are ever written. Endurance and wilderness are not in the
exports. A value of 0 means "not rated" and is never written. Paved-road
segments are never tagged (ITRS itself presents no rating on them).
"""

from __future__ import annotations

from .itrs import NOT_RATED, Segment

TECH_KEY = "mtb:scale:itrs:technical_difficulty"
EXPOSURE_KEY = "mtb:scale:itrs:exposure"
ITRS_KEYS = (TECH_KEY, EXPOSURE_KEY)
ITRS_KEY_PREFIX = "mtb:scale:itrs:"

TECH_VALUES = range(1, 6)
EXPOSURE_VALUES = range(1, 5)

# Names per itrs.bike and the ITRS rating data explanation.
TECH_NAMES = {1: "Beginner", 2: "Intermediate", 3: "Advanced", 4: "Expert", 5: "Extreme"}
EXPOSURE_NAMES = {
    1: "Normal injury risk",
    2: "High risk of serious injury",
    3: "Life threatening consequences",
    4: "Fatal consequences",
}

# Official ITRS palette (ITRS Colour Palette.gpl). 0 draws as the road purple.
LEVEL_COLORS = {
    0: "#844c82",
    1: "#72bf44",
    2: "#0080c6",
    3: "#ee3124",
    4: "#000000",
    5: "#f68b1f",
}
COLOR_UNMATCHED = "#595959"


def level_name(key: str, value: int | str) -> str:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return "?"
    names = TECH_NAMES if key == TECH_KEY else EXPOSURE_NAMES
    return names.get(v, "not rated" if v == NOT_RATED else "?")


def target_tags(segment: Segment) -> dict[str, str]:
    """The tags a segment asks for: only rated keys, nothing on paved roads."""
    if segment.is_paved_road:
        return {}
    tags: dict[str, str] = {}
    if segment.tech_rating in TECH_VALUES:
        tags[TECH_KEY] = str(segment.tech_rating)
    if segment.max_exposure in EXPOSURE_VALUES:
        tags[EXPOSURE_KEY] = str(segment.max_exposure)
    return tags


def skipped_keys(segment: Segment) -> list[tuple[str, str]]:
    """(key, reason) for every rating the segment carries but can't write."""
    out: list[tuple[str, str]] = []
    if segment.is_paved_road:
        reason = "paved road: ITRS ratings are not applied to surfaced roads"
        return [(TECH_KEY, reason), (EXPOSURE_KEY, reason)]
    if segment.tech_rating == NOT_RATED:
        out.append((TECH_KEY, "not rated (0)"))
    elif segment.tech_rating not in TECH_VALUES:
        out.append((TECH_KEY, f"value {segment.tech_rating} outside 1-5"))
    if segment.max_exposure == NOT_RATED:
        out.append((EXPOSURE_KEY, "not rated (0)"))
    elif segment.max_exposure not in EXPOSURE_VALUES:
        out.append((EXPOSURE_KEY, f"value {segment.max_exposure} outside 1-4"))
    return out


def existing_itrs(tags: dict[str, str]) -> dict[str, str]:
    """Every mtb:scale:itrs:* tag already on an element."""
    return {k: v for k, v in tags.items() if k.startswith(ITRS_KEY_PREFIX)}
