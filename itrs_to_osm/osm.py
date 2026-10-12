"""In-memory OSM data model, read from Overpass XML and written as a JOSM file.

The tool downloads an area with Overpass ``out meta``, edits it in memory
(splits ways, adds nodes and ways, tags pieces, updates relations), and
writes everything back in JOSM's ``.osm`` format for a human to review:
https://wiki.openstreetmap.org/wiki/JOSM_file_format

JOSM refuses to upload a modified element without its ``version``, so the
metadata of every downloaded element (version, timestamp, changeset, uid,
user) is kept exactly as read and written back untouched. Modified existing
elements carry ``action='modify'``. New elements get negative ids and no
metadata at all; JOSM treats a negative id as "new".

Relations downloaded with Overpass ``<`` usually reference members that are
not in the file. That is normal (JOSM shows them as incomplete), so nothing
here assumes a member is present.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ACTION_MODIFY = "modify"
ACTION_DELETE = "delete"
_ACTIONS = (ACTION_MODIFY, ACTION_DELETE)


@dataclass
class Node:
    id: int
    lat: float
    lon: float
    tags: dict[str, str] = field(default_factory=dict)
    version: int | None = None
    timestamp: str | None = None
    changeset: int | None = None
    uid: int | None = None
    user: str | None = None
    visible: bool = True
    action: str | None = None  # None, "modify", or "delete"


@dataclass
class Way:
    id: int
    nodes: list[int]  # node ids in order; see OsmData.set_way_nodes
    tags: dict[str, str] = field(default_factory=dict)
    version: int | None = None
    timestamp: str | None = None
    changeset: int | None = None
    uid: int | None = None
    user: str | None = None
    visible: bool = True
    action: str | None = None


@dataclass
class Member:
    type: str  # "node" | "way" | "relation"
    ref: int
    role: str = ""


@dataclass
class Relation:
    id: int
    members: list[Member]
    tags: dict[str, str] = field(default_factory=dict)
    version: int | None = None
    timestamp: str | None = None
    changeset: int | None = None
    uid: int | None = None
    user: str | None = None
    visible: bool = True
    action: str | None = None


@dataclass
class Bounds:
    minlat: float
    minlon: float
    maxlat: float
    maxlon: float
    origin: str | None = None


Element = Node | Way | Relation


def element_type(element: Element) -> str:
    """The OSM type name ("node", "way", "relation") of an element."""
    if isinstance(element, Node):
        return "node"
    if isinstance(element, Way):
        return "way"
    if isinstance(element, Relation):
        return "relation"
    raise TypeError(f"not an OSM element: {element!r}")


def _sort_key(element_id: int) -> tuple[int, int]:
    # New elements first in creation order (-1, -2, ...), then existing ascending.
    return (0, -element_id) if element_id < 0 else (1, element_id)


@dataclass
class OsmData:
    """Nodes, ways, and relations keyed by id, plus download bounds.

    Equality compares map content (elements and bounds) only. ``generator``
    and ``osm_base`` describe where the data came from, not what it is, and
    the JOSM format has no place for ``osm_base``, so they're left out.

    ``parent_ways`` and ``parent_relations`` use indexes built lazily on
    first use. ``add``, ``merge``, and ``set_way_nodes`` keep them right.
    Code that changes ``way.nodes`` or ``relation.members`` directly must
    call ``invalidate_index()`` afterwards.
    """

    nodes: dict[int, Node] = field(default_factory=dict)
    ways: dict[int, Way] = field(default_factory=dict)
    relations: dict[int, Relation] = field(default_factory=dict)
    bounds: list[Bounds] = field(default_factory=list)
    generator: str | None = field(default=None, compare=False)
    osm_base: str | None = field(default=None, compare=False)
    _node_ways: dict[int, list[int]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _member_of: dict[tuple[str, int], list[int]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _next_new_id: int = field(default=-1, init=False, repr=False, compare=False)

    def _table(self, element: Element) -> dict[int, Element]:
        kind = element_type(element)
        if kind == "node":
            return self.nodes  # type: ignore[return-value]
        if kind == "way":
            return self.ways  # type: ignore[return-value]
        return self.relations  # type: ignore[return-value]

    def add(self, element: Element) -> None:
        """Add or replace an element by id."""
        self._table(element)[element.id] = element
        self.invalidate_index()

    def merge(self, other: OsmData) -> None:
        """Union ``other`` into this dataset by element id.

        Where both hold the same id, the higher version wins (ties keep
        this dataset's copy), so overlapping Overpass downloads taken a few
        minutes apart end up with the newest data. Bounds are concatenated.
        ``osm_base`` becomes the older of the two, so the staleness of the
        oldest download is never hidden. Meant for downloaded data; merging
        two datasets that both hold new (negative id) elements would mix
        unrelated objects.
        """
        for mine, theirs in (
            (self.nodes, other.nodes),
            (self.ways, other.ways),
            (self.relations, other.relations),
        ):
            for element_id, element in theirs.items():
                current = mine.get(element_id)
                if current is None or (element.version or 0) > (current.version or 0):
                    mine[element_id] = element  # type: ignore[assignment]
        self.bounds.extend(other.bounds)
        if self.osm_base and other.osm_base:
            self.osm_base = min(self.osm_base, other.osm_base)
        else:
            self.osm_base = self.osm_base or other.osm_base
        if self.generator is None:
            self.generator = other.generator
        self.invalidate_index()

    def new_id(self) -> int:
        """The next unused negative id, unique across all three element types."""
        candidate = self._next_new_id
        while candidate in self.nodes or candidate in self.ways or candidate in self.relations:
            candidate -= 1
        self._next_new_id = candidate - 1
        return candidate

    def invalidate_index(self) -> None:
        """Drop the parent indexes; they're rebuilt on next use."""
        self._node_ways = None
        self._member_of = None

    def set_way_nodes(self, way: Way, nodes: list[int]) -> None:
        """Replace a way's node list and keep the parent index right."""
        way.nodes = list(nodes)
        self.invalidate_index()

    def _build_node_ways(self) -> dict[int, list[int]]:
        index: dict[int, list[int]] = {}
        for way in self.ways.values():
            for node_id in dict.fromkeys(way.nodes):  # closed ways list a node twice
                index.setdefault(node_id, []).append(way.id)
        return index

    def _build_member_of(self) -> dict[tuple[str, int], list[int]]:
        index: dict[tuple[str, int], list[int]] = {}
        for relation in self.relations.values():
            for key in dict.fromkeys((m.type, m.ref) for m in relation.members):
                index.setdefault(key, []).append(relation.id)
        return index

    def parent_ways(self, node_id: int) -> list[Way]:
        """Ways in this dataset that contain the node."""
        if self._node_ways is None:
            self._node_ways = self._build_node_ways()
        return [self.ways[w] for w in self._node_ways.get(node_id, [])]

    def parent_relations(self, element: Element) -> list[Relation]:
        """Relations in this dataset that have the element as a member."""
        if self._member_of is None:
            self._member_of = self._build_member_of()
        key = (element_type(element), element.id)
        return [self.relations[r] for r in self._member_of.get(key, [])]

    def way_coords(self, way: Way | int) -> list[tuple[float, float]]:
        """(lon, lat) of each node of a way, in order.

        Raises KeyError naming the node id when a node isn't in the dataset.
        """
        if isinstance(way, int):
            way = self.ways[way]
        coords: list[tuple[float, float]] = []
        for node_id in way.nodes:
            node = self.nodes.get(node_id)
            if node is None:
                raise KeyError(f"node {node_id} of way {way.id} is not in the data")
            coords.append((node.lon, node.lat))
        return coords

    @staticmethod
    def is_new(element: Element) -> bool:
        return element.id < 0

    def mark_modified(self, element: Element) -> None:
        """Flag an existing element for upload; new elements need no flag."""
        if not self.is_new(element):
            element.action = ACTION_MODIFY

    def modified(self) -> list[Element]:
        """Every element JOSM would upload: action set, or a negative id."""
        out: list[Element] = []
        for table in (self.nodes, self.ways, self.relations):
            for element_id in sorted(table, key=_sort_key):
                element = table[element_id]
                if element.action is not None or element_id < 0:
                    out.append(element)
        return out


# --- Reading ---------------------------------------------------------------


def _opt_int(attrs: dict[str, str], key: str) -> int | None:
    value = attrs.get(key)
    return int(value) if value not in (None, "") else None


def _common(el: ET.Element) -> dict:
    """Attributes shared by node, way, and relation."""
    attrs = el.attrib
    action = attrs.get("action")
    if action is not None and action not in _ACTIONS:
        raise ValueError(f"{el.tag} {attrs.get('id')}: unknown action {action!r}")
    return {
        "id": int(attrs["id"]),
        "tags": {t.attrib["k"]: t.attrib["v"] for t in el.findall("tag")},
        "version": _opt_int(attrs, "version"),
        "timestamp": attrs.get("timestamp"),
        "changeset": _opt_int(attrs, "changeset"),
        "uid": _opt_int(attrs, "uid"),
        "user": attrs.get("user"),
        "visible": attrs.get("visible", "true") != "false",
        "action": action,
    }


def read_osm_xml(text: str | bytes) -> OsmData:
    """Parse OSM XML from Overpass (``out meta``) or a JOSM-saved file.

    Raises ValueError for an Overpass ``<remark>`` (a runtime error such as
    a timeout, returned with HTTP 200 and partial data) and for anything
    that isn't an ``<osm>`` document. Documents with a DOCTYPE are refused
    outright, as neither source produces one and entity expansion is the
    usual XML attack.
    """
    head = text[:4096] if isinstance(text, str) else text[:4096].decode("utf-8", "replace")
    if "<!DOCTYPE" in head.upper():
        raise ValueError("OSM XML with a DOCTYPE is not accepted")
    try:
        root = ET.fromstring(text, parser=ET.XMLParser())
    except ET.ParseError as e:
        raise ValueError(f"not well-formed OSM XML: {e}") from e
    if root.tag != "osm":
        raise ValueError(f"expected an <osm> document, got <{root.tag}>")

    data = OsmData(generator=root.attrib.get("generator"))
    for el in root:
        if el.tag == "remark":
            raise ValueError(f"Overpass error: {(el.text or '').strip()}")
        if el.tag == "meta":
            data.osm_base = el.attrib.get("osm_base")
        elif el.tag == "bounds":
            a = el.attrib
            data.bounds.append(
                Bounds(
                    minlat=float(a["minlat"]),
                    minlon=float(a["minlon"]),
                    maxlat=float(a["maxlat"]),
                    maxlon=float(a["maxlon"]),
                    origin=a.get("origin"),
                )
            )
        elif el.tag == "node":
            data.nodes[int(el.attrib["id"])] = Node(
                lat=float(el.attrib["lat"]), lon=float(el.attrib["lon"]), **_common(el)
            )
        elif el.tag == "way":
            nodes = [int(nd.attrib["ref"]) for nd in el.findall("nd")]
            data.ways[int(el.attrib["id"])] = Way(nodes=nodes, **_common(el))
        elif el.tag == "relation":
            members = [
                Member(
                    type=m.attrib["type"], ref=int(m.attrib["ref"]), role=m.attrib.get("role", "")
                )
                for m in el.findall("member")
            ]
            data.relations[int(el.attrib["id"])] = Relation(members=members, **_common(el))
        # <note> and anything else unknown is ignored.
    return data


def read_osm_file(path: str | Path) -> OsmData:
    # Parse bytes so the document's own encoding declaration is honored.
    return read_osm_xml(Path(path).read_bytes())


# --- Writing ---------------------------------------------------------------


def _coord(value: float) -> str:
    """Up to 7 decimals (OSM's precision), no trailing zeros, never exponent form."""
    text = f"{value:.7f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _element_xml(parent: ET.Element, element: Element) -> None:
    kind = element_type(element)
    el = ET.SubElement(parent, kind)
    el.set("id", str(element.id))
    if element.id >= 0:
        # Attribute order follows JOSM's own writer.
        if element.action is not None:
            el.set("action", element.action)
        if element.timestamp is not None:
            el.set("timestamp", element.timestamp)
        if element.uid is not None:
            el.set("uid", str(element.uid))
        if element.user is not None:
            el.set("user", element.user)
        el.set("visible", "true" if element.visible else "false")
        if element.version is not None:
            el.set("version", str(element.version))
        if element.changeset is not None:
            el.set("changeset", str(element.changeset))
    if isinstance(element, Node):
        el.set("lat", _coord(element.lat))
        el.set("lon", _coord(element.lon))
    elif isinstance(element, Way):
        for ref in element.nodes:
            ET.SubElement(el, "nd", ref=str(ref))
    else:
        for m in element.members:
            ET.SubElement(el, "member", type=m.type, ref=str(m.ref), role=m.role)
    for k, v in element.tags.items():
        ET.SubElement(el, "tag", k=k, v=v)


def to_josm_xml(data: OsmData, *, generator: str) -> str:
    """Serialize as a JOSM ``.osm`` file, deterministically ordered.

    Nodes, then ways, then relations; within each, new elements first
    (-1, -2, ...), then existing ones by ascending id.
    """
    root = ET.Element("osm", version="0.6", generator=generator, upload="true")
    for b in data.bounds:
        bounds = ET.SubElement(
            root,
            "bounds",
            minlat=_coord(b.minlat),
            minlon=_coord(b.minlon),
            maxlat=_coord(b.maxlat),
            maxlon=_coord(b.maxlon),
        )
        if b.origin is not None:
            bounds.set("origin", b.origin)
    for table in (data.nodes, data.ways, data.relations):
        for element_id in sorted(table, key=_sort_key):
            _element_xml(root, table[element_id])
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return "<?xml version='1.0' encoding='UTF-8'?>\n" + body + "\n"


def write_josm_file(data: OsmData, path: str | Path, *, generator: str) -> None:
    Path(path).write_text(to_josm_xml(data, generator=generator), encoding="utf-8", newline="\n")
