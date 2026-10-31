# research

This directory holds local, private material that never goes into git. Only
this README is committed; `.gitignore` excludes everything else here.

The layout I use:

- `exports/`: real ITRS GeoJSON exports from trail raters. Raters and ITRS
  share these privately, so they stay local.
  `tests/test_private_samples.py` runs on every `*.geojson` here, and skips
  when there are none.
- `itrs-docs/`: reference material from ITRS (the rating data explanation
  PDF, the QGIS assets, and the schema notes). The PDF is available for
  download from itrs.bike (https://itrs.bike/downloads/). Downloading it
  means accepting the ITRS licensing conditions
  (https://itrs.bike/licensing-conditions/), so get your own copy rather
  than passing this one around. The QGIS assets came directly from ITRS
  and aren't published.
- `output/`: `.osm` files and reports from runs against the real exports.
- `project_startup.txt`: the original project brief.

If you're collaborating and have your own exports, put them in `exports/`
and the private-sample tests will pick them up. Those tests also need the
Overpass download for each track's area in `./cache`. So run the tool once on
each export by itself (eg: `itrs-to-osm research/exports/<file> --dry-run`)
before running `pytest`. A batch run merges overlapping areas into one
download, which the tests won't find.
