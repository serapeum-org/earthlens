"""OpenStreetMap feature backend over two live query protocols.

`earthlens.osm` fetches OpenStreetMap features through two public, keyless
protocols and returns them as a pyramids
`~pyramids.feature.collection.FeatureCollection` (CRS `EPSG:4326`):

* **Overpass** (`overpy`) — small/targeted **current-state** features by bbox +
  tag filter (Overpass QL).
* **ohsome** (`ohsome`) — OSM **history + analytics** (the `elements/geometry`
  endpoint) over a time range.

A request names a curated **named query** (`variables=["overpass:hospitals"]`,
`variables=["ohsome:buildings"]`) plus a bbox; the backend routes to the
protocol, runs the live query, converts the result to a `FeatureCollection`,
and warns about OSM's **ODbL** share-alike licence. A raw `query=` (Overpass
QL) / `filter=` (ohsome) override is accepted for power users.

This is a `vector` backend (`OUTPUT_KIND = "vector"`), so the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument. Both
protocols are public — there is **no auth class** and the SDKs are imported
lazily, so the package imports without `earthlens[osm]`. Bulk OSM PBF / planet
extracts and ohsome's aggregation endpoints are out of scope (follow-ons).

Public surface (re-exported from this package):

* `OSM` — the backend; instantiate with `variables=[query_id]` + a bbox, then
  call `OSM.download`.
* `Catalog` — pydantic-backed loader for the bundled `osm_data_catalog.yaml`
  named-query dispatch table.
* `Dataset` — one named query's dispatch row (`protocol`, `query_template` /
  `ohsome_filter`, `geometry_types`, `description`).
* `bbox_swne` / `bbox_wsen` / `shapely_bbox` — the bbox-order helpers (`G3`).
* `overpy_to_gdf` / `to_fc` / `empty_fc` — the result → `GeoDataFrame` →
  `FeatureCollection` converters (`G4` / `G7`).
* `LicenseWarning` — the ODbL share-alike warning (`G5`).
* `CATALOG_PATH` — path to the bundled named-query YAML; monkey-patchable in
  tests.

Examples:
    - List a couple of the curated named queries:

        ```python
        >>> from earthlens.osm import Catalog
        >>> cat = Catalog()
        >>> "overpass:hospitals" in cat and "ohsome:buildings" in cat
        True

        ```
"""

from __future__ import annotations

from earthlens.osm._helpers import (
    LicenseWarning,
    bbox_swne,
    bbox_wsen,
    empty_fc,
    overpy_to_gdf,
    shapely_bbox,
    to_fc,
)
from earthlens.osm.backend import OSM
from earthlens.osm.catalog import CATALOG_PATH, Catalog, Dataset

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Dataset",
    "LicenseWarning",
    "OSM",
    "bbox_swne",
    "bbox_wsen",
    "empty_fc",
    "overpy_to_gdf",
    "shapely_bbox",
    "to_fc",
]
