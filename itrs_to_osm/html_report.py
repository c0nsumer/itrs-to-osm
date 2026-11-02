"""The HTML report: the same blocks as the Markdown report, plus a Leaflet map.

The page is one self-contained file. The only outside requests are the
Leaflet CSS and JS (pinned on unpkg) and CyclOSM raster tiles. The OSM
Foundation's standard tiles need an HTTP Referer, which a page opened from
file:// never sends, so they come back as the "Blocked" tile; CyclOSM
(hosted by OpenStreetMap France) serves without one and renders mtb:scale,
which suits a trail review. The map shows the
ITRS track as a thin dashed line and each OSM way piece that received tags
as a thick line in the official ITRS color for its technical difficulty.
Unmatched segments of the track are grey and paved-road segments are the
ITRS road purple. Geometry goes into the page as a JSON literal.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .itrs import NOT_RATED
from .report import (
    OSM_WAY_URL,
    Block,
    Code,
    CodeBlock,
    Heading,
    Inline,
    ItemList,
    Link,
    Para,
    RunContext,
    Strong,
    Table,
    build_report,
    inline_parts,
    node_ref,
    piece_ref,
    piece_title,
    point_detail_text,
    point_placement_text,
    point_status_text,
    rating_text,
    segment_label,
    segment_status,
    way_ref,
)
from .tagging import (
    COLOR_UNMATCHED,
    EXPOSURE_KEY,
    EXPOSURE_NAMES,
    LEVEL_COLORS,
    TECH_KEY,
    TECH_NAMES,
    existing_itrs,
)

LEAFLET_VERSION = "1.9.4"
LEAFLET_CSS = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css"
LEAFLET_JS = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="

COLOR_TRACK = "#555555"
COLOR_POINT = "#ffffff"  # an ITRS point this tool wrote nothing for
COLOR_NOT_RATED = LEVEL_COLORS[NOT_RATED]  # ITRS draws roads and 0 in purple

_e = html.escape


# --- Blocks to HTML -----------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _inline(content: Inline) -> str:
    out: list[str] = []
    for part in inline_parts(content):
        if isinstance(part, Strong):
            out.append(f"<strong>{_e(part.text)}</strong>")
        elif isinstance(part, Code):
            out.append(f"<code>{_e(part.text)}</code>")
        elif isinstance(part, Link):
            out.append(
                f'<a href="{_e(part.url, quote=True)}" target="_blank" rel="noopener">'
                f"{_e(part.text)}</a>"
            )
        else:
            out.append(_e(part))
    return "".join(out)


def render_html_blocks(blocks: list[Block]) -> str:
    out: list[str] = []
    used: set[str] = set()
    for block in blocks:
        if isinstance(block, Heading):
            slug = base = _slug(block.text)
            n = 2
            while slug in used:
                slug, n = f"{base}-{n}", n + 1
            used.add(slug)
            out.append(f'<h{block.level} id="{slug}">{_e(block.text)}</h{block.level}>')
        elif isinstance(block, Para):
            out.append(f"<p>{_inline(block.content)}</p>")
        elif isinstance(block, ItemList):
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{_inline(i)}</li>" for i in block.items)
            out.append(f"<{tag}>{items}</{tag}>")
        elif isinstance(block, Table):
            head = "".join(f"<th>{_e(h)}</th>" for h in block.header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in block.rows
            )
            out.append(
                f'<div class="table"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>"
            )
        elif isinstance(block, CodeBlock):
            out.append(f"<pre>{_e(block.text)}</pre>")
    return "\n".join(out)


# --- Map data -----------------------------------------------------------------


def _latlon(coords: list[tuple[float, ...]]) -> list[list[float]]:
    """GeoJSON-order (lon, lat, ...) to Leaflet [lat, lon]."""
    return [[round(c[1], 7), round(c[0], 7)] for c in coords]


def _popup(title: str, lines: list[str], link: str | None = None) -> str:
    """Popup HTML; every piece of text is escaped here."""
    parts = [f"<b>{_e(title)}</b>"] + [_e(line) for line in lines]
    if link:
        parts.append(
            f'<a href="{_e(link, quote=True)}" target="_blank" rel="noopener">{_e(link)}</a>'
        )
    return "<br>".join(parts)


def _tags_text(tags: dict[str, str]) -> str:
    itrs = existing_itrs(tags)
    return "; ".join(f"{k}={v}" for k, v in sorted(itrs.items())) or "no ITRS tags"


def _tech_color(tags: dict[str, str]) -> str:
    try:
        return LEVEL_COLORS[int(tags.get(TECH_KEY, ""))]
    except (ValueError, KeyError):
        return COLOR_NOT_RATED


def map_data(ctx: RunContext) -> dict[str, Any]:
    """Lines (in draw order) and points for the Leaflet map, plus bounds."""
    pieces: list[dict[str, Any]] = []
    tracks: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    lats: list[float] = []
    lons: list[float] = []

    def add_bounds(latlon: list[list[float]]) -> None:
        lats.extend(p[0] for p in latlon)
        lons.extend(p[1] for p in latlon)

    for w in ctx.result.ways:
        if w.status == "refused":
            try:
                coords = _latlon(ctx.data.way_coords(w.way_id))
            except KeyError:
                continue
            lines = [f"refused: {r.key} is {r.old}, survey says {r.new}" for r in w.refusals]
            pieces.append(
                {
                    "coords": coords,
                    "style": {"color": COLOR_UNMATCHED, "weight": 5, "dashArray": "2 8"},
                    "popup": _popup(
                        f"{way_ref(w.way_id)} {w.name}".strip(), lines, OSM_WAY_URL.format(w.way_id)
                    ),
                }
            )
            add_bounds(coords)
            continue
        for p in w.pieces:
            if not existing_itrs(p.tags_after):
                continue  # a piece outside the track: nothing to show
            try:
                coords = _latlon(ctx.data.way_coords(p.way_id))
            except KeyError:
                continue
            exposure = p.tags_after.get(EXPOSURE_KEY)
            lines = [
                f"{w.highway or 'no highway tag'}, {p.length:.0f} m, {w.status}",
                f"tags after: {_tags_text(p.tags_after)}",
                "exposure: "
                + (rating_text(EXPOSURE_KEY, int(exposure)) if exposure else "not tagged"),
                f"from: {', '.join(p.sources) or '-'}",
            ]
            title = f"{piece_ref(p.way_id, p.keeps_id and len(w.pieces) > 1)} {w.name}".strip()
            link = OSM_WAY_URL.format(p.way_id) if p.way_id > 0 else None
            pieces.append(
                {
                    "coords": coords,
                    "style": {"color": _tech_color(p.tags_after), "weight": 5, "opacity": 0.9},
                    "popup": _popup(title, lines, link),
                }
            )
            add_bounds(coords)

    changes = {id(c.point): c for c in ctx.points.changes}
    for tm in ctx.matches:
        for sm in tm.segments:
            seg = sm.segment
            coords = _latlon(seg.coords)
            add_bounds(coords)
            status = segment_status(sm)
            lines = [
                f"tech {rating_text(TECH_KEY, seg.tech_rating)}, "
                f"exposure {rating_text(EXPOSURE_KEY, seg.max_exposure)}",
                f"{seg.track_type}, {seg.use}, {seg.direction}",
                f"{status}; ways: {', '.join(way_ref(i) for i in sm.way_ids) or 'none'}",
                *sm.reasons,
            ]
            popup = _popup(segment_label(tm.track, seg), lines)
            if status == "unmatched":
                style = {"color": COLOR_UNMATCHED, "weight": 4, "opacity": 0.9}
            elif seg.is_paved_road:
                style = {"color": COLOR_NOT_RATED, "weight": 4, "opacity": 0.8}
            else:
                style = {"color": COLOR_TRACK, "weight": 2, "opacity": 0.9, "dashArray": "4 6"}
            tracks.append({"coords": coords, "style": style, "popup": popup})
        for pt in tm.track.points:
            seg = tm.track.segment_by_uuid(pt.segment_uuid)
            lines = [f"segment {seg.name if seg else pt.segment_name or '?'}"]
            photos = "".join(
                f'<br><a href="{_e(u, quote=True)}" target="_blank" rel="noopener">photo {i}</a>'
                for i, u in enumerate(pt.photos, 1)
            )
            lat, lon, color = pt.lat, pt.lon, COLOR_POINT
            change = changes.get(id(pt))
            if change is None:
                if pt.value not in ("", None):
                    lines.append(f"value: {pt.value}")
                if pt.remark:
                    lines.append(f"remark: {pt.remark}")
            else:
                value = change.value
                lines.append(
                    "exposure: " + (rating_text(EXPOSURE_KEY, value) if value is not None else "?")
                )
                node = ctx.data.nodes.get(change.node_id) if change.node_id is not None else None
                if node is not None and (change.written or change.status == "already"):
                    lat, lon = node.lat, node.lon
                    color = LEVEL_COLORS.get(value or 0, COLOR_POINT)
                if change.node_id is not None:
                    lines.append(f"node: {node_ref(change.node_id)}")
                    lines.append(f"placed: {point_placement_text(change)}")
                if change.way_id is not None:
                    lines.append(f"way: {piece_title(ctx, change.way_id)}")
                lines.append(f"status: {point_status_text(change)}")
                if detail := point_detail_text(pt):
                    lines.append(f"ITRS: {detail}")
            points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "color": color,
                    "written": color != COLOR_POINT,
                    "popup": _popup(f"{tm.track.name}: {pt.point_type or 'point'}", lines) + photos,
                }
            )
            lats.append(lat)
            lons.append(lon)

    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]] if lats else None
    return {"lines": pieces + tracks, "points": points, "bounds": bounds}


def _legend() -> str:
    """Three groups: the surveyed ITRS track, the OSM ways tagged, and the nodes tagged."""
    track_rows = [
        (COLOR_TRACK, "Surveyed segment", "dashed"),
        (COLOR_NOT_RATED, "Paved road segment (not tagged)", "thin"),
        (COLOR_UNMATCHED, "Unmatched segment (not tagged)", "thin"),
    ]
    way_rows = [(LEVEL_COLORS[v], f"Tech {v} {TECH_NAMES[v]}", "") for v in sorted(TECH_NAMES)]
    way_rows += [
        (COLOR_NOT_RATED, "Exposure only (tech not rated)", ""),
        (COLOR_UNMATCHED, "Refused (existing value differs)", "dotted"),
    ]

    node_rows = [
        (LEVEL_COLORS[v], f"Exposure {v} {EXPOSURE_NAMES[v]}", "dot")
        for v in sorted(EXPOSURE_NAMES)
    ]
    node_rows.append((COLOR_POINT, "ITRS point (not written)", "dot"))

    def group(title: str, rows: list[tuple[str, str, str]]) -> str:
        items = "".join(
            f'<div><span class="swatch {cls}" style="background:{_e(color)}"></span>{_e(label)}</div>'
            for color, label, cls in rows
        )
        return f"<b>{_e(title)}</b>{items}"

    return (
        '<div class="legend">'
        + group("ITRS track (as surveyed)", track_rows)
        + '<div class="gap"></div>'
        + group("OSM ways with tags applied", way_rows)
        + '<div class="gap"></div>'
        + group("OSM nodes with exposure applied", node_rows)
        + "</div>"
    )


def _script_json(value: Any) -> str:
    """JSON safe to embed in a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


_MAP_JS = """
const map = L.map("map");
L.tileLayer("https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '<a href="https://www.cyclosm.org/">CyclOSM</a> | Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
for (const line of MAP_DATA.lines) {
  L.polyline(line.coords, line.style).bindPopup(line.popup).addTo(map);
}
for (const p of MAP_DATA.points) {
  L.circleMarker([p.lat, p.lon], p.written
    ? {radius: 7, color: "#222", weight: 2, fillColor: p.color, fillOpacity: 1}
    : {radius: 6, color: "#222", weight: 1, fillColor: p.color, fillOpacity: 0.9})
    .bindPopup(p.popup).addTo(map);
}
if (MAP_DATA.bounds) {
  map.fitBounds(MAP_DATA.bounds, {padding: [20, 20]});
} else {
  map.setView([0, 0], 2);
}
const legend = L.control({position: "bottomright"});
legend.onAdd = () => {
  const div = L.DomUtil.create("div");
  div.innerHTML = LEGEND_HTML;
  return div;
};
legend.addTo(map);
"""

_CSS = """
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0 auto;
  max-width: 1200px; padding: 0 16px 48px; color: #1b1b1b; background: #fff; line-height: 1.45; }
h1 { margin-top: 24px; }
h2 { margin-top: 36px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
h3 { margin-top: 24px; font-size: 1.05em; }
#map { height: 60vh; min-height: 320px; border: 1px solid #ccc; margin: 16px 0; }
.table { overflow-x: auto; }
table { border-collapse: collapse; font-size: 0.9em; margin: 8px 0; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #f3f3f3; }
pre { background: #f6f6f6; border: 1px solid #ddd; padding: 8px; overflow-x: auto;
  white-space: pre-wrap; }
code { background: #f3f3f3; padding: 0 3px; }
.legend { background: #fff; padding: 6px 8px; border-radius: 4px; font-size: 12px;
  line-height: 1.6; box-shadow: 0 1px 4px rgba(0,0,0,0.3); }
.swatch { display: inline-block; width: 18px; height: 5px; margin-right: 6px;
  vertical-align: middle; }
.swatch.thin { height: 3px; }
.swatch.dashed { height: 0; background: none !important; border-top: 2px dashed #555; }
.swatch.dotted { height: 0; background: none !important; border-top: 5px dotted #595959; }
.swatch.dot { width: 10px; height: 10px; border-radius: 50%; border: 1px solid #222;
  margin: 0 10px 0 4px; }
.legend .gap { height: 6px; }
"""


def render_html(ctx: RunContext) -> str:
    blocks = build_report(ctx)
    split = next(
        (i for i, b in enumerate(blocks) if isinstance(b, Heading) and b.level == 2),
        len(blocks),
    )
    title = "itrs-to-osm report: " + ", ".join(t.name for t in ctx.tracks)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<link rel="stylesheet" href="{LEAFLET_CSS}" integrity="{LEAFLET_CSS_SRI}" crossorigin="">
<script src="{LEAFLET_JS}" integrity="{LEAFLET_JS_SRI}" crossorigin=""></script>
<style>{_CSS}</style>
</head>
<body>
{render_html_blocks(blocks[:split])}
<div id="map"></div>
{render_html_blocks(blocks[split:])}
<script>
const MAP_DATA = {_script_json(map_data(ctx))};
const LEGEND_HTML = {_script_json(_legend())};
{_MAP_JS}
</script>
</body>
</html>
"""


def write_html(ctx: RunContext, path: Path) -> None:
    """Write the HTML report with the map."""
    path.write_text(render_html(ctx), encoding="utf-8")
