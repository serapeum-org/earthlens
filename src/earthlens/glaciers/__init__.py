"""Glacier data backend (`earthlens.glaciers`).

One mixed backend over three open glacier sources:

* **RGI 7.0** (Randolph Glacier Inventory) — global glacier *outlines*, one
  shapefile per GTN-G first-order region, served openly by UNESCO IHP-WINS; the
  backend maps the request bbox to the overlapping region(s), downloads + caches
  each region ZIP, reads it via pyramids `FeatureCollection.read_file`, and clips
  to the bbox; `vector`.
* **GLIMS** (Global Land Ice Measurements from Space) — time-series glacier
  outlines, queried by bbox through the open GLIMS GeoServer WFS; `vector`.
* **WGMS** — Fluctuations of Glaciers database (mass balance, front variation /
  length change, glacier state) as tabular CSV tables; `tabular`.

Output is **per instance**: a dataset's `output_kind` decides whether
:meth:`~earthlens.glaciers.backend.Glaciers.download` returns a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` (`vector`, rgi/glims) or
a :class:`pandas.DataFrame` (`tabular`, wgms). All three sources are open, so the
backend ships **no auth**. Vector file I/O always goes through pyramids
`FeatureCollection.read_file` — never a bare geopandas file read — and the WGMS
path is pure pandas with no array/NetCDF stack.

The public surface is the :class:`Catalog` (dataset id -> source + output kind +
request detail, plus the GTN-G :class:`Region` table) and its :class:`Dataset`
rows, the backend :class:`Glaciers`, and the stateless query/read helpers.
"""

from __future__ import annotations

from earthlens.glaciers._helpers import (
    concat_outlines,
    download_zip,
    empty_canonical,
    fetch_glims,
    glims_wfs_url,
    parse_wgms_csv,
    read_outlines,
    regions_for_bbox,
    shapely_bbox,
    wgms_glacier_table,
)
from earthlens.glaciers.catalog import Catalog, Dataset, Region

__all__ = [
    "Catalog",
    "Dataset",
    "Region",
    "shapely_bbox",
    "regions_for_bbox",
    "download_zip",
    "read_outlines",
    "fetch_glims",
    "glims_wfs_url",
    "concat_outlines",
    "parse_wgms_csv",
    "wgms_glacier_table",
    "empty_canonical",
]
