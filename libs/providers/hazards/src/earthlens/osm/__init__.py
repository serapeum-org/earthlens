"""OpenStreetMap feature backend over three query protocols.

`earthlens.osm` fetches OpenStreetMap features through three public, keyless
protocols and returns them as a pyramids
`~pyramids.feature.collection.FeatureCollection` (CRS `EPSG:4326`):

* **Overpass** (`overpy`) — small/targeted **current-state** features by bbox +
  tag filter (Overpass QL).
* **ohsome** (`ohsome`) — OSM **history + analytics** (the `elements/geometry`
  endpoint) over a time range.
* **pbf** (`pyrosm` / `pyosmium`) — **bulk / regional** reads from a Geofabrik
  `.osm.pbf` extract (`G9`): fetch-and-cache the extract, read a layer
  (buildings / roads / pois / …) with `pyrosm` (in-memory) or `pyosmium`
  (streaming, planet-scale), and clip to the request bbox.

A request names a curated **named query** (`variables=["overpass:hospitals"]`,
`variables=["ohsome:buildings"]`, `variables=["pbf:buildings"]`) plus a bbox
(and, for `pbf`, a `region=` Geofabrik key); the backend routes to the
protocol, produces the features, converts them to a `FeatureCollection`, and
warns about OSM's **ODbL** share-alike licence. A raw `query=` (Overpass QL) /
`filter=` (ohsome) override is accepted for power users.

This is a `vector` backend (`OUTPUT_KIND = "vector"`), so the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument. All
three protocols are public — there is **no auth class**, and the SDKs are
imported lazily, so the package imports without `earthlens[osm]` /
`earthlens[osm-pbf]`. ohsome's aggregation endpoints remain out of scope.

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
* `download_extract` / `read_pbf` / `geofabrik_url` / `GEOFABRIK_BASE_URL` —
  the `pbf` fetch-and-cache + layer-read helpers (`G13` / `G14`).
* `LicenseWarning` — the ODbL share-alike warning (`G5`).
* `OhsomeResponseError` / `OhsomeUnavailableError` — the typed errors the ohsome
  path raises instead of a raw `JSONDecodeError`: `OhsomeResponseError` for any
  non-JSON body (carrying the status / `Content-Type` / body preview, `#930`),
  and its `OhsomeUnavailableError` subtype for the `403` / `429` throttle/block.
* `ohsome_http_status` / `ohsome_error_response` / `ohsome_response_is_non_json`
  / `ohsome_body_preview` — the helpers that recover the HTTP status, response,
  non-JSON verdict, and body preview from the SDK's opaque failure.
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
    OhsomeResponseError,
    OhsomeUnavailableError,
    bbox_swne,
    bbox_wsen,
    empty_fc,
    ohsome_body_preview,
    ohsome_error_response,
    ohsome_http_status,
    ohsome_response_is_non_json,
    overpy_to_gdf,
    shapely_bbox,
    to_fc,
)
from earthlens.osm._pbf import (
    GEOFABRIK_BASE_URL,
    download_extract,
    geofabrik_url,
    read_pbf,
)
from earthlens.osm.backend import OSM
from earthlens.osm.catalog import CATALOG_PATH, Catalog, Dataset

__all__ = [
    "CATALOG_PATH",
    "GEOFABRIK_BASE_URL",
    "Catalog",
    "Dataset",
    "LicenseWarning",
    "OSM",
    "OhsomeResponseError",
    "OhsomeUnavailableError",
    "bbox_swne",
    "bbox_wsen",
    "download_extract",
    "empty_fc",
    "geofabrik_url",
    "ohsome_body_preview",
    "ohsome_error_response",
    "ohsome_http_status",
    "ohsome_response_is_non_json",
    "overpy_to_gdf",
    "read_pbf",
    "shapely_bbox",
    "to_fc",
]
