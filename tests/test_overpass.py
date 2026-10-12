"""Overpass client tests. Offline: a fake session stands in for the network."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from itrs_to_osm import overpass
from itrs_to_osm.overpass import (
    DEFAULT_ENDPOINT,
    HTTP_TIMEOUT_S,
    QUERY_TIMEOUT_S,
    RETRY_BACKOFF_S,
    USER_AGENT,
    OverpassError,
    build_query,
    cache_key,
    fetch,
    fetch_area,
)

OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API 0.7.62.7 375dc00a">
<note>The data included in this document is from www.openstreetmap.org.</note>
<meta osm_base="2026-10-11T12:34:56Z"/>
  <node id="1" lat="47.45" lon="-87.80" version="3"/>
  <way id="10" version="7"><nd ref="1"/><tag k="highway" v="path"/></way>
</osm>
"""

REMARK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API 0.7.62.7 375dc00a">
<meta osm_base="2026-10-11T12:34:56Z"/>
<remark> runtime error: Query timed out in "query" at line 2 after 91 seconds. </remark>
</osm>
"""

BUSY_HTML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN">
<html><head><title>OSM3S Response</title></head><body>
<p><strong style="color:#FF0000">Error</strong>: runtime error: open64: 0 Success
/osm3s_osm_base Dispatcher_Client::request_read_and_idx::timeout.
The server is probably too busy to handle your request.</p>
</body></html>
"""

BBOX = (-87.81, 47.44, -87.79, 47.46)


@dataclass
class FakeResponse:
    status_code: int
    text: str

    def raise_for_status(self) -> None:  # pragma: no cover - the client must not rely on it
        raise AssertionError("raise_for_status should not be called")


@dataclass
class FakeSession:
    """Replays queued responses (or raises queued exceptions) and records each call."""

    responses: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post(self, url: str, *, data: Any, headers: Any, timeout: Any) -> FakeResponse:
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(overpass, "_sleep", recorded.append)
    return recorded


def ok(text: str = OSM_XML) -> FakeResponse:
    return FakeResponse(200, text)


def test_build_query_uses_south_west_north_east():
    q = build_query(BBOX)
    assert q.startswith(f"[out:xml][timeout:{QUERY_TIMEOUT_S}];")
    assert 'way["highway"](47.4400000,-87.8100000,47.4600000,-87.7900000)->.w;' in q
    assert "(.w; .w>; .w<;);" in q
    assert q.rstrip().endswith("out meta;")


def test_build_query_never_uses_scientific_notation():
    q = build_query((-0.00000001, 0.00000002, 0.00001, 1e-5))
    assert "e-" not in q
    assert "(0.0000000,-0.0000000,0.0000100,0.0000100)" in q


def test_http_timeout_exceeds_query_timeout():
    assert HTTP_TIMEOUT_S > QUERY_TIMEOUT_S


def test_cache_miss_fetches_and_writes_both_files(tmp_path, sleeps):
    sess = FakeSession([ok()])
    text = fetch("Q", cache_dir=tmp_path, session=sess)
    assert text == OSM_XML
    assert len(sess.calls) == 1
    call = sess.calls[0]
    assert call["url"] == DEFAULT_ENDPOINT
    assert call["data"] == {"data": "Q"}
    assert call["headers"] == {"User-Agent": USER_AGENT}
    assert call["timeout"] == HTTP_TIMEOUT_S

    key = cache_key("Q", DEFAULT_ENDPOINT)
    assert (tmp_path / f"{key}.osm").read_text(encoding="utf-8") == OSM_XML
    meta = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert meta["endpoint"] == DEFAULT_ENDPOINT
    assert meta["query"] == "Q"
    assert meta["osm_base"] == "2026-10-11T12:34:56Z"
    assert meta["fetched_at"].endswith("+00:00")
    assert sleeps == []


def test_cache_key_depends_on_endpoint():
    assert cache_key("Q", "https://a.example/api") != cache_key("Q", "https://b.example/api")


def test_cache_hit_does_not_touch_network(tmp_path, sleeps):
    fetch("Q", cache_dir=tmp_path, session=FakeSession([ok()]))
    sess = FakeSession([])
    assert fetch("Q", cache_dir=tmp_path, session=sess) == OSM_XML
    assert sess.calls == []


def test_refresh_skips_read_and_rewrites(tmp_path, sleeps):
    fetch("Q", cache_dir=tmp_path, session=FakeSession([ok()]))
    newer = OSM_XML.replace("2026-10-11T12:34:56Z", "2026-10-12T00:00:00Z")
    sess = FakeSession([ok(newer)])
    assert fetch("Q", cache_dir=tmp_path, refresh=True, session=sess) == newer
    assert len(sess.calls) == 1
    key = cache_key("Q", DEFAULT_ENDPOINT)
    assert (tmp_path / f"{key}.osm").read_text(encoding="utf-8") == newer
    meta = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert meta["osm_base"] == "2026-10-12T00:00:00Z"


def test_no_cache_dir_disables_caching(tmp_path, sleeps):
    sess = FakeSession([ok(), ok()])
    fetch("Q", cache_dir=None, session=sess)
    fetch("Q", cache_dir=None, session=sess)
    assert len(sess.calls) == 2


def test_unreadable_cache_entry_is_refetched(tmp_path, sleeps):
    key = cache_key("Q", DEFAULT_ENDPOINT)
    (tmp_path / f"{key}.osm").write_text("<osm trunc", encoding="utf-8")
    sess = FakeSession([ok()])
    assert fetch("Q", cache_dir=tmp_path, session=sess) == OSM_XML
    assert len(sess.calls) == 1


def test_retries_429_then_succeeds(tmp_path, sleeps):
    sess = FakeSession([FakeResponse(429, "rate limited"), ok()])
    assert fetch("Q", cache_dir=tmp_path, session=sess) == OSM_XML
    assert len(sess.calls) == 2
    assert sleeps == [RETRY_BACKOFF_S[0]]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_retries_gateway_errors(status, sleeps):
    sess = FakeSession([FakeResponse(status, "gateway"), ok()])
    assert fetch("Q", cache_dir=None, session=sess) == OSM_XML
    assert len(sess.calls) == 2


def test_retries_connection_errors_and_timeouts(sleeps):
    sess = FakeSession([requests.ConnectionError("refused"), requests.Timeout("slow"), ok()])
    assert fetch("Q", cache_dir=None, session=sess) == OSM_XML
    assert sleeps == RETRY_BACKOFF_S[:2]


def test_retries_busy_server_html_then_succeeds(tmp_path, sleeps):
    sess = FakeSession([ok(BUSY_HTML), ok()])
    assert fetch("Q", cache_dir=tmp_path, session=sess) == OSM_XML
    assert len(sess.calls) == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("status", [400, 406])
def test_client_error_raises_without_retry(tmp_path, sleeps, status):
    sess = FakeSession([FakeResponse(status, "Error: line 2: parse error " + "x" * 1000)])
    with pytest.raises(OverpassError) as exc:
        fetch("Q", cache_dir=tmp_path, session=sess)
    msg = str(exc.value)
    assert f"HTTP {status}" in msg
    assert "parse error" in msg
    assert len(msg) < 500
    assert len(sess.calls) == 1
    assert sleeps == []
    assert list(tmp_path.iterdir()) == []


def test_remark_raises_and_is_not_cached(tmp_path, sleeps):
    sess = FakeSession([ok(REMARK_XML)])
    with pytest.raises(OverpassError, match="Query timed out"):
        fetch("Q", cache_dir=tmp_path, session=sess)
    assert len(sess.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_schedule_exhausted_raises(tmp_path, sleeps):
    attempts = len(RETRY_BACKOFF_S) + 1
    sess = FakeSession([FakeResponse(504, "gateway timeout")] * attempts)
    with pytest.raises(OverpassError, match="504"):
        fetch("Q", cache_dir=tmp_path, session=sess)
    assert len(sess.calls) == attempts
    assert sleeps == RETRY_BACKOFF_S
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_leaves_no_tmp(tmp_path, sleeps):
    cache = tmp_path / "nested" / "cache"
    fetch("Q", cache_dir=cache, session=FakeSession([ok()]))
    names = sorted(p.name for p in cache.iterdir())
    assert len(names) == 2
    assert [n.rsplit(".", 1)[1] for n in names] == ["json", "osm"]
    assert not any(n.endswith(".tmp") for n in names)


def test_fetch_area_builds_query_and_fetches(tmp_path, sleeps):
    sess = FakeSession([ok()])
    assert fetch_area(BBOX, cache_dir=tmp_path, session=sess) == OSM_XML
    assert sess.calls[0]["data"] == {"data": build_query(BBOX)}
    assert (tmp_path / f"{cache_key(build_query(BBOX), DEFAULT_ENDPOINT)}.osm").exists()
