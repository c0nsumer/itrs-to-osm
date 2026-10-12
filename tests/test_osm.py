from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from itrs_to_osm.osm import (
    Bounds,
    Member,
    Node,
    OsmData,
    Relation,
    Way,
    read_osm_file,
    read_osm_xml,
    to_josm_xml,
    write_josm_file,
)

# Overpass-style `out meta` output: two ways sharing junction node 3, way 10
# in a route relation whose other member (way 999) isn't in the file, a
# non-ASCII user name, and a tag value with &, ", ', and a newline.
OVERPASS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API 0.7.62.1 084b4234">
<note>The data included in this document is from www.openstreetmap.org.</note>
<meta osm_base="2026-10-11T12:00:00Z"/>
  <bounds minlat="47.4500000" minlon="-87.8100000" maxlat="47.4600000" maxlon="-87.7900000"/>
  <node id="1" lat="47.4500000" lon="-87.8000000" version="3" timestamp="2024-05-01T10:00:00Z" changeset="111" uid="42" user="Jürgen Müller"/>
  <node id="2" lat="47.4501000" lon="-87.7999000" version="1" timestamp="2024-05-01T10:00:00Z" changeset="111" uid="42" user="Jürgen Müller"/>
  <node id="3" lat="47.4502000" lon="-87.7998000" version="2" timestamp="2024-05-02T10:00:00Z" changeset="112" uid="7" user="mapper">
    <tag k="highway" v="trailhead"/>
  </node>
  <node id="4" lat="47.4503000" lon="-87.7997000" version="1" timestamp="2024-05-02T10:00:00Z" changeset="112" uid="7" user="mapper"/>
  <node id="5" lat="47.4504000" lon="-87.7999500" version="1" timestamp="2024-05-02T10:00:00Z" changeset="112" uid="7" user="mapper"/>
  <way id="10" version="5" timestamp="2025-01-01T00:00:00Z" changeset="200" uid="42" user="Jürgen Müller">
    <nd ref="1"/>
    <nd ref="2"/>
    <nd ref="3"/>
    <tag k="highway" v="path"/>
    <tag k="name" v="Loam Rider"/>
    <tag k="note" v="Tom &amp; Jerry said &quot;hi&quot; &apos;twice&apos;&#10;second line"/>
  </way>
  <way id="11" version="1" timestamp="2025-01-02T00:00:00Z" changeset="201" uid="7" user="mapper">
    <nd ref="3"/>
    <nd ref="4"/>
    <nd ref="5"/>
    <tag k="highway" v="track"/>
  </way>
  <relation id="100" version="9" timestamp="2025-02-01T00:00:00Z" changeset="300" uid="7" user="mapper">
    <member type="way" ref="10" role=""/>
    <member type="way" ref="999" role="forward"/>
    <tag k="type" v="route"/>
    <tag k="route" v="mtb"/>
  </relation>
</osm>
"""


@pytest.fixture
def data() -> OsmData:
    return read_osm_xml(OVERPASS_XML)


def test_parses_elements_and_metadata(data):
    assert sorted(data.nodes) == [1, 2, 3, 4, 5]
    assert sorted(data.ways) == [10, 11]
    assert sorted(data.relations) == [100]
    n1 = data.nodes[1]
    assert (n1.lat, n1.lon) == (47.45, -87.8)
    assert n1.version == 3
    assert n1.timestamp == "2024-05-01T10:00:00Z"
    assert n1.changeset == 111
    assert n1.uid == 42
    assert n1.user == "Jürgen Müller"
    assert n1.visible is True
    assert n1.action is None
    assert data.nodes[3].tags == {"highway": "trailhead"}
    w = data.ways[10]
    assert w.nodes == [1, 2, 3]
    assert list(w.tags) == ["highway", "name", "note"]
    assert w.tags["note"] == "Tom & Jerry said \"hi\" 'twice'\nsecond line"
    r = data.relations[100]
    assert r.members == [Member("way", 10, ""), Member("way", 999, "forward")]
    assert r.tags == {"type": "route", "route": "mtb"}
    assert data.generator.startswith("Overpass API")


def test_bounds_and_osm_base(data):
    assert data.bounds == [Bounds(47.45, -87.81, 47.46, -87.79)]
    assert data.osm_base == "2026-10-11T12:00:00Z"


def test_reads_josm_style_attributes():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='JOSM' upload='true'>
  <bounds minlat='1' minlon='2' maxlat='3' maxlon='4' origin='OpenStreetMap server'/>
  <node id='-1' lat='1.5' lon='2.5'/>
  <node id='7' action='delete' visible='false' version='2' lat='1' lon='2'/>
  <way id='8' action='modify' version='4'><nd ref='-1'/><nd ref='7'/></way>
</osm>
"""
    data = read_osm_xml(xml)
    assert data.bounds[0].origin == "OpenStreetMap server"
    new = data.nodes[-1]
    assert new.version is None and new.timestamp is None and new.user is None
    assert data.nodes[7].visible is False
    assert data.nodes[7].action == "delete"
    assert data.ways[8].action == "modify"
    assert data.osm_base is None


def test_remark_raises():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API">
<remark> runtime error: Query timed out in "query" at line 3 after 26 seconds. </remark>
</osm>"""
    with pytest.raises(ValueError, match="Query timed out"):
        read_osm_xml(xml)


def test_rejects_non_osm_and_doctype():
    with pytest.raises(ValueError):
        read_osm_xml("<gpx/>")
    with pytest.raises(ValueError):
        read_osm_xml('<!DOCTYPE osm [<!ENTITY a "b">]><osm version="0.6">&a;</osm>')
    with pytest.raises(ValueError):
        read_osm_xml("<osm")


def test_parent_ways(data):
    assert [w.id for w in data.parent_ways(3)] == [10, 11]
    assert [w.id for w in data.parent_ways(1)] == [10]
    assert data.parent_ways(12345) == []


def test_parent_ways_closed_way_listed_once(data):
    data.add(Way(id=12, nodes=[4, 5, 4]))
    assert [w.id for w in data.parent_ways(4)] == [11, 12]


def test_parent_relations_with_incomplete_relation(data):
    assert [r.id for r in data.parent_relations(data.ways[10])] == [100]
    assert data.parent_relations(data.ways[11]) == []
    assert data.parent_relations(data.nodes[1]) == []
    # Way 999 isn't downloaded, but the relation still references it.
    assert 999 not in data.ways
    assert [r.id for r in data.parent_relations(Way(id=999, nodes=[]))] == [100]


def test_way_coords(data):
    assert data.way_coords(10) == [(-87.8, 47.45), (-87.7999, 47.4501), (-87.7998, 47.4502)]
    assert data.way_coords(data.ways[11])[0] == (-87.7998, 47.4502)


def test_way_coords_missing_node(data):
    data.add(Way(id=13, nodes=[1, 777]))
    with pytest.raises(KeyError, match="777"):
        data.way_coords(13)


def test_new_id_unique_across_types_and_skips_existing(data):
    assert data.new_id() == -1
    data.add(Node(id=-2, lat=0, lon=0))
    data.add(Relation(id=-3, members=[]))
    data.add(Way(id=-5, nodes=[]))
    assert data.new_id() == -4
    assert data.new_id() == -6
    # Ids handed out are not reused even if the caller hasn't added them yet.
    assert data.new_id() == -7


def test_mark_modified_and_is_new(data):
    way = data.ways[10]
    data.mark_modified(way)
    assert way.action == "modify"
    new = Way(id=data.new_id(), nodes=[3, 4])
    data.add(new)
    data.mark_modified(new)
    assert new.action is None
    assert data.is_new(new) and not data.is_new(way)


def test_modified(data):
    assert data.modified() == []
    data.mark_modified(data.ways[11])
    data.mark_modified(data.relations[100])
    node = Node(id=data.new_id(), lat=47.45015, lon=-87.79985)
    data.add(node)
    assert [(type(e).__name__, e.id) for e in data.modified()] == [
        ("Node", -1),
        ("Way", 11),
        ("Relation", 100),
    ]


def test_merge_keeps_higher_version_and_concatenates_bounds(data):
    other = read_osm_xml(OVERPASS_XML)
    other.osm_base = "2026-10-11T11:00:00Z"
    other.bounds = [Bounds(46.0, 7.0, 46.1, 7.1)]
    other.ways[10].version = 6
    other.ways[10].tags["surface"] = "dirt"
    other.ways[11].tags["stale"] = "same version, should not win"
    other.add(Node(id=50, lat=46.05, lon=7.05, version=1))
    data.osm_base = "2026-10-11T12:30:00Z"
    data.merge(other)
    assert data.ways[10].version == 6
    assert data.ways[10].tags["surface"] == "dirt"
    assert "stale" not in data.ways[11].tags
    assert 50 in data.nodes
    assert len(data.bounds) == 2
    assert data.osm_base == "2026-10-11T11:00:00Z"


def test_merge_osm_base_when_only_one_set():
    a = OsmData()
    b = OsmData(osm_base="2026-10-11T12:00:00Z")
    a.merge(b)
    assert a.osm_base == "2026-10-11T12:00:00Z"


def test_merge_invalidates_index(data):
    assert [w.id for w in data.parent_ways(5)] == [11]
    other = OsmData()
    other.add(Way(id=20, nodes=[5, 6], version=1))
    data.merge(other)
    assert [w.id for w in data.parent_ways(5)] == [11, 20]


def test_set_way_nodes_keeps_index(data):
    assert [w.id for w in data.parent_ways(3)] == [10, 11]
    # Split way 10 at node 2: 10 keeps [1, 2], new way gets [2, 3].
    piece = Way(id=data.new_id(), nodes=[2, 3], tags=dict(data.ways[10].tags))
    data.set_way_nodes(data.ways[10], [1, 2])
    data.add(piece)
    assert [w.id for w in data.parent_ways(3)] == [11, -1]
    assert sorted(w.id for w in data.parent_ways(2)) == [-1, 10]


def test_direct_mutation_needs_invalidate(data):
    assert [w.id for w in data.parent_ways(1)] == [10]
    data.ways[10].nodes.remove(1)
    data.invalidate_index()
    assert data.parent_ways(1) == []


def _edited(data: OsmData) -> OsmData:
    data.ways[10].tags["mtb:scale:itrs:technical_difficulty"] = "2"
    data.mark_modified(data.ways[10])
    node = Node(id=data.new_id(), lat=47.45015, lon=-87.79985)
    data.add(node)
    way = Way(id=data.new_id(), nodes=[node.id, 2], tags={"highway": "path"})
    data.add(way)
    return data


def test_josm_xml_actions_and_metadata(data):
    out = to_josm_xml(_edited(data), generator="itrs-to-osm/0.1.0")
    assert out.startswith("<?xml version='1.0' encoding='UTF-8'?>\n")
    assert 'generator="itrs-to-osm/0.1.0"' in out
    assert 'upload="true"' in out
    assert '<bounds minlat="47.45" minlon="-87.81" maxlat="47.46" maxlon="-87.79"' in out
    root = ET.fromstring(out.split("\n", 1)[1])
    ways = {int(w.get("id")): w for w in root.findall("way")}
    assert ways[10].get("action") == "modify"
    assert ways[10].get("version") == "5"
    assert ways[11].get("action") is None
    assert ways[11].get("visible") == "true"
    assert ways[11].get("user") == "mapper"
    for el in root.findall("*"):
        if el.tag in ("node", "way", "relation") and int(el.get("id")) < 0:
            for attr in ("version", "timestamp", "changeset", "uid", "user", "action"):
                assert el.get(attr) is None
    new_node = root.find("node[@id='-1']")
    assert (new_node.get("lat"), new_node.get("lon")) == ("47.45015", "-87.79985")
    assert [nd.get("ref") for nd in ways[-2].findall("nd")] == ["-1", "2"]
    member = root.find("relation/member[@ref='999']")
    assert member.attrib == {"type": "way", "ref": "999", "role": "forward"}


def test_josm_xml_deterministic_order(data):
    out = to_josm_xml(_edited(data), generator="g")
    root = ET.fromstring(out.split("\n", 1)[1])
    order = [(el.tag, int(el.get("id"))) for el in root if el.tag != "bounds"]
    assert order == [
        ("node", -1),
        ("node", 1),
        ("node", 2),
        ("node", 3),
        ("node", 4),
        ("node", 5),
        ("way", -2),
        ("way", 10),
        ("way", 11),
        ("relation", 100),
    ]
    assert to_josm_xml(data, generator="g") == out


def test_coordinates_never_use_exponent():
    data = OsmData()
    data.add(Node(id=1, lat=1e-8, lon=-0.00001, version=1))
    out = to_josm_xml(data, generator="g")
    assert 'lat="0"' in out
    assert 'lon="-0.00001"' in out
    assert "e-" not in out


def test_round_trip(data):
    again = read_osm_xml(to_josm_xml(data, generator="itrs-to-osm/test"))
    assert again == data
    assert again.ways[10].tags["note"] == data.ways[10].tags["note"]
    assert again.nodes[1].user == "Jürgen Müller"


def test_round_trip_with_edits(data):
    edited = _edited(data)
    again = read_osm_xml(to_josm_xml(edited, generator="g"))
    assert again == edited


def test_file_round_trip(data, tmp_path):
    path = tmp_path / "out.osm"
    write_josm_file(data, path, generator="itrs-to-osm/test")
    assert path.read_bytes().startswith(b"<?xml version='1.0' encoding='UTF-8'?>\n")
    assert "Jürgen".encode() in path.read_bytes()
    again = read_osm_file(path)
    assert again == data
    assert again.generator == "itrs-to-osm/test"
