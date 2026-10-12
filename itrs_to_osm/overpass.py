"""Download OSM data for an area from the Overpass API, with caching and retry.

Follows the conventions of trailmaps.app-map-generator's ``overpass.py``: one
endpoint, a descriptive User-Agent, a patient retry schedule, and a cache
keyed by query hash so reruns are offline and reproducible. Only reads from
Overpass; nothing here ever writes to OSM.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from itrs_to_osm import __version__

log = logging.getLogger("itrs_to_osm.overpass")

# Single canonical endpoint. The bare host load-balances across its backends
# and stays within minutes of OSM; public mirrors replicate independently and
# can silently serve snapshots weeks behind, so they are not used.
DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"

# The front host rejects `python-requests/*` User-Agents with 406. A
# descriptive one gets through and tells the operator who to contact.
USER_AGENT = f"itrs-to-osm/{__version__} (+https://github.com/c0nsumer/itrs-to-osm)"

# Server-side budget, written into the query as [timeout:N].
QUERY_TIMEOUT_S = 90
# Client-side HTTP timeout. It must exceed the server budget, or the client
# hangs up on a legitimately slow query and every retry restarts the work.
HTTP_TIMEOUT_S = QUERY_TIMEOUT_S + 15

# Seconds to wait between attempts: ramps up to two minutes. There is one
# more attempt than there are entries (no sleep after the last attempt).
RETRY_BACKOFF_S = [5, 10, 20, 40, 60, 90, 120]

# Statuses worth retrying: rate limiting and gateway/overload errors. Any
# other non-200 means the request itself was refused (400 bad query, 406
# refused User-Agent), so retrying cannot help.
RETRY_STATUSES = frozenset({429, 502, 503, 504})

# How much of an unexpected body to quote in an error message.
BODY_EXCERPT_CHARS = 300

# Patched to a no-op by tests.
_sleep = time.sleep

# An OSM XML document's root element. Overpass's own HTML error pages also
# begin with an <?xml prolog, so the prolog alone does not prove success.
_OSM_ROOT_RE = re.compile(r"<osm[\s>]")
_REMARK_RE = re.compile(r"<remark>(.*?)</remark>", re.DOTALL)
_OSM_BASE_RE = re.compile(r"<meta\b[^>]*\bosm_base=\"([^\"]*)\"")


class OverpassError(Exception):
    """The Overpass API refused the query, reported an error, or never answered."""


class _Retryable(Exception):
    """A transient failure: wait and try again."""


def _fmt(value: float) -> str:
    # Fixed-point: repr() could produce scientific notation near zero.
    return f"{value:.7f}"


def build_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for every highway way in ``bbox``, plus their nodes and parent relations.

    ``bbox`` is (min_lon, min_lat, max_lon, max_lat), already buffered by the
    caller. Overpass wants south,west,north,east. ``out meta`` is required
    because JOSM needs each object's ``version`` to upload a modification.
    Parent relations come along so splitting a way can keep route relations
    whole.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    box = ",".join(_fmt(v) for v in (min_lat, min_lon, max_lat, max_lon))
    return (
        f"[out:xml][timeout:{QUERY_TIMEOUT_S}];\n"
        f'way["highway"]({box})->.w;\n'
        "(.w; .w>; .w<;);\n"
        "out meta;\n"
    )


def cache_key(query: str, endpoint: str = DEFAULT_ENDPOINT) -> str:
    """Hash naming the cache entry. The endpoint is included so two servers never share one."""
    return hashlib.sha256(f"{endpoint}\n{query}".encode()).hexdigest()


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > BODY_EXCERPT_CHARS:
        return text[:BODY_EXCERPT_CHARS] + "..."
    return text


def _body_text(resp: object) -> str:
    """Decode the response body as UTF-8.

    Overpass labels XML as ``application/osm3s+xml`` with no charset, so
    ``requests`` would guess the encoding; the document itself declares
    UTF-8. Falls back to ``.text`` for objects without ``.content``.
    """
    content = getattr(resp, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return resp.text  # type: ignore[attr-defined]


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp sibling and os.replace, so a crash never leaves a truncated file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_cache(osm_path: Path) -> str | None:
    if not osm_path.exists():
        return None
    try:
        text = osm_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("Discarding unreadable Overpass cache %s (%s)", osm_path, e)
        return None
    if not (_OSM_ROOT_RE.search(text) and text.rstrip().endswith("</osm>")):
        log.warning("Discarding Overpass cache %s: not a complete OSM XML document", osm_path)
        return None
    return text


def _write_cache(osm_path: Path, text: str, endpoint: str, query: str) -> None:
    match = _OSM_BASE_RE.search(text)
    meta = {
        "endpoint": endpoint,
        "query": query,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "osm_base": match.group(1) if match else None,
    }
    try:
        osm_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(osm_path, text)
        _write_atomic(osm_path.with_suffix(".json"), json.dumps(meta, indent=2) + "\n")
    except OSError as e:
        # A cache failure costs a re-download next time; it shouldn't fail the run.
        log.warning("Could not write Overpass cache %s: %s", osm_path, e)


def _attempt(sess: requests.Session, endpoint: str, query: str) -> str:
    """One POST. Returns OSM XML, raises _Retryable or OverpassError."""
    try:
        resp = sess.post(
            endpoint,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
        )
    except (requests.ConnectionError, requests.Timeout) as e:
        raise _Retryable(f"{type(e).__name__}: {e}") from e

    body = _body_text(resp)
    if resp.status_code in RETRY_STATUSES:
        raise _Retryable(f"HTTP {resp.status_code}: {_excerpt(body)}")
    if resp.status_code != 200:
        raise OverpassError(
            f"Overpass refused the query (HTTP {resp.status_code}): {_excerpt(body)}"
        )

    # An overloaded dispatcher answers 200 with an HTML page ("The server is
    # probably too busy to handle your request") instead of an OSM document.
    if not _OSM_ROOT_RE.search(body):
        raise _Retryable(f"HTTP 200 but not OSM XML: {_excerpt(body)}")

    # A runtime error (query timeout, memory exhaustion) arrives as a <remark>
    # inside an otherwise valid but truncated document. Using it would drop
    # ways silently, so it's an error, and it is never cached.
    remark = _REMARK_RE.search(body)
    if remark:
        text = html.unescape(remark.group(1)).strip()
        raise OverpassError(f"Overpass reported an error: {text}")
    return body


def fetch(
    query: str,
    *,
    cache_dir: Path | str | None,
    refresh: bool = False,
    endpoint: str = DEFAULT_ENDPOINT,
    session: requests.Session | None = None,
) -> str:
    """Return the OSM XML text for ``query``, from the cache or from Overpass.

    The cache entry is ``<sha256>.osm`` under ``cache_dir`` with a sibling
    ``<sha256>.json`` recording the endpoint, query, fetch time, and the
    server's ``osm_base`` timestamp (what the report cites as the data's
    as-of). A cached entry is served regardless of age, so reruns are
    offline and reproducible; re-downloading is an explicit ``refresh``,
    which skips the read but still rewrites the entry. ``cache_dir=None``
    disables caching.
    """
    osm_path = Path(cache_dir) / f"{cache_key(query, endpoint)}.osm" if cache_dir else None
    if osm_path is not None and not refresh:
        cached = _read_cache(osm_path)
        if cached is not None:
            log.info("Using cached Overpass response %s", osm_path)
            return cached
    elif osm_path is not None and osm_path.exists():
        log.info("Bypassing cached Overpass response (refresh requested): %s", osm_path)

    sess = session if session is not None else requests.Session()
    try:
        text = _fetch_with_retry(sess, endpoint, query)
    finally:
        if session is None:
            sess.close()
    if osm_path is not None:
        _write_cache(osm_path, text, endpoint, query)
    return text


def _fetch_with_retry(sess: requests.Session, endpoint: str, query: str) -> str:
    attempts = len(RETRY_BACKOFF_S) + 1
    last_error = ""
    for attempt in range(attempts):
        log.info("Querying %s (attempt %d/%d)", endpoint, attempt + 1, attempts)
        try:
            return _attempt(sess, endpoint, query)
        except _Retryable as e:
            last_error = str(e)
            log.warning("Overpass attempt %d failed: %s", attempt + 1, last_error)
            if attempt < attempts - 1:
                delay = RETRY_BACKOFF_S[attempt]
                log.info("Retrying in %ds", delay)
                _sleep(delay)

    raise OverpassError(
        f"Overpass did not answer after {attempts} attempts. Last failure: {last_error}"
    )


def fetch_area(
    bbox: tuple[float, float, float, float],
    *,
    cache_dir: Path | str | None,
    refresh: bool = False,
    endpoint: str = DEFAULT_ENDPOINT,
    session: requests.Session | None = None,
) -> str:
    """Download every highway way in ``bbox`` (see build_query) as OSM XML."""
    return fetch(
        build_query(bbox),
        cache_dir=cache_dir,
        refresh=refresh,
        endpoint=endpoint,
        session=session,
    )
