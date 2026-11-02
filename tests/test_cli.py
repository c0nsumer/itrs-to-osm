"""End-to-end runs of the command line tool on synthetic data (no network)."""

from __future__ import annotations

import json

import pytest
from fixtures.builder import SCENARIOS

from itrs_to_osm.cli import EXIT_ERROR, EXIT_OK, EXIT_REFUSED, main
from itrs_to_osm.osm import read_osm_file
from itrs_to_osm.tagging import EXPOSURE_KEY, TECH_KEY, existing_itrs


def write_scenario(tmp_path, name, *, stem="geojson-export_Test Trail"):
    xml, export, expect = SCENARIOS[name]()
    osm = tmp_path / "area.osm"
    osm.write_text(xml, encoding="utf-8")
    geojson = tmp_path / f"{stem}.geojson"
    geojson.write_text(json.dumps(export), encoding="utf-8")
    return osm, geojson, expect


def test_single_file_writes_osm_and_report(tmp_path, capsys):
    osm, geojson, expect = write_scenario(tmp_path, "single_way_three_segments")
    rc = main([str(geojson), "--osm", str(osm)])
    assert rc == EXIT_OK
    out_osm = tmp_path / "Test Trail.osm"  # the geojson-export_ prefix is dropped
    report = tmp_path / "Test Trail.report.md"
    assert out_osm.exists() and report.exists()
    data = read_osm_file(out_osm)
    trail = expect["ways"]["trail"]
    assert data.ways[trail].action == "modify"
    new_ways = [w for w in data.ways.values() if w.id < 0]
    assert len(new_ways) == 1
    assert existing_itrs(new_ways[0].tags) == {TECH_KEY: "3", "mtb:scale:itrs:exposure": "1"}
    assert "itrs-to-osm/" in data.generator
    out = capsys.readouterr().out
    assert "3 matched" in out and "wrote" in out


def test_dry_run_writes_report_only(tmp_path):
    osm, geojson, _ = write_scenario(tmp_path, "single_way_uniform")
    assert main([str(geojson), "--osm", str(osm), "--dry-run"]) == EXIT_OK
    assert not (tmp_path / "Test Trail.osm").exists()
    assert (tmp_path / "Test Trail.report.md").exists()


def test_refusal_exit_code_and_force(tmp_path, capsys):
    osm, geojson, expect = write_scenario(tmp_path, "existing_itrs_tags")
    rc = main([str(geojson), "--osm", str(osm)])
    assert rc == EXIT_REFUSED
    assert "refused" in capsys.readouterr().err
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert data.ways[expect["ways"]["trail"]].action is None
    rc = main([str(geojson), "--osm", str(osm), "--force"])
    assert rc == EXIT_OK
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert existing_itrs(data.ways[expect["ways"]["trail"]].tags)[TECH_KEY] == "3"


def test_several_files_need_out_name_and_share_one_output(tmp_path):
    osm, a, _ = write_scenario(tmp_path, "single_way_uniform", stem="a")
    _, b, _ = write_scenario(tmp_path, "single_way_uniform", stem="b")
    assert main([str(a), str(b), "--osm", str(osm)]) == EXIT_ERROR
    assert main([str(a), str(b), "--osm", str(osm), "--out", "batch"]) == EXIT_OK
    assert (tmp_path / "batch.osm").exists()
    assert (tmp_path / "batch.report.md").exists()


def test_out_dir_and_bad_input(tmp_path):
    osm, geojson, _ = write_scenario(tmp_path, "single_way_uniform")
    out_dir = tmp_path / "nested" / "out"
    assert main([str(geojson), "--osm", str(osm), "--out-dir", str(out_dir)]) == EXIT_OK
    assert (out_dir / "Test Trail.osm").exists()
    bad = tmp_path / "bad.geojson"
    bad.write_text("{}", encoding="utf-8")
    assert main([str(bad), "--osm", str(osm)]) == EXIT_ERROR


def test_way_id_restriction_flag(tmp_path):
    osm, geojson, expect = write_scenario(tmp_path, "track_spans_two_ways")
    first = expect["ways"]["first"]
    assert main([str(geojson), "--osm", str(osm), "--way-id", str(first)]) == EXIT_OK
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert existing_itrs(data.ways[first].tags)
    assert not existing_itrs(data.ways[expect["ways"]["second"]].tags)
    assert main([str(geojson), "--osm", str(osm), "--way-id", "x"]) == EXIT_ERROR


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_runs_through_the_cli(tmp_path, name):
    osm, geojson, expect = write_scenario(tmp_path, name)
    rc = main([str(geojson), "--osm", str(osm)])
    point_refused = any(p["status"] == "refused" for p in expect.get("exposure_nodes", []))
    assert rc == (EXIT_REFUSED if expect["refused"] or point_refused else EXIT_OK)
    assert (tmp_path / "Test Trail.osm").exists()


def test_exposure_min_out_of_range_is_an_error(tmp_path, capsys):
    osm, geojson, _ = write_scenario(tmp_path, "boundary_near_existing_node")
    assert main([str(geojson), "--osm", str(osm), "--exposure-min", "7"]) == EXIT_ERROR
    assert "--exposure-min" in capsys.readouterr().err
    assert not (tmp_path / "Test Trail.osm").exists()


def test_exposure_nodes_summary_and_switch(tmp_path, capsys):
    osm, geojson, _ = write_scenario(tmp_path, "boundary_near_existing_node")
    assert main([str(geojson), "--osm", str(osm)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "1 exposure node(s) tagged" in out
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert [n.tags for n in data.nodes.values() if EXPOSURE_KEY in n.tags] == [{EXPOSURE_KEY: "2"}]

    assert main([str(geojson), "--osm", str(osm), "--no-exposure-nodes"]) == EXIT_OK
    assert "0 exposure node(s) tagged" in capsys.readouterr().out
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert not any(EXPOSURE_KEY in n.tags for n in data.nodes.values())


def test_exposure_point_refusal_exits_refused_unless_forced(tmp_path, capsys):
    osm, geojson, expect = write_scenario(tmp_path, "exposure_points")
    node = expect["tagged_node_id"]
    assert main([str(geojson), "--osm", str(osm)]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert "3 exposure node(s) tagged" in captured.out
    assert "refused: 1" in captured.err
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert data.nodes[node].tags == {EXPOSURE_KEY: "3"} and data.nodes[node].action is None
    assert main([str(geojson), "--osm", str(osm), "--force"]) == EXIT_OK
    assert "4 exposure node(s) tagged" in capsys.readouterr().out
    data = read_osm_file(tmp_path / "Test Trail.osm")
    assert data.nodes[node].tags == {EXPOSURE_KEY: "2"} and data.nodes[node].action == "modify"
