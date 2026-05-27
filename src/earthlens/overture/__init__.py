"""Overture Maps Foundation backend (vector GeoParquet over public S3).

Thin wrapper over the official `overturemaps` SDK, which reads the
Overture Maps Foundation 1.0 GeoParquet on the public, anonymous
`s3://overturemaps-us-west-2` bucket. A request is a **theme + bbox**
(+ optional release): the backend fetches the bbox-pushed-down
GeoParquet for each requested feature type, surfaces a per-row
`license_id`, and returns the result as a pyramids
`~pyramids.feature.collection.FeatureCollection` written to disk
(CRS `EPSG:4326`).

This is a `vector` backend: the result is a table of features
(building footprints, POIs, road segments, admin boundaries), not a
gridded array, so `Overture.OUTPUT_KIND` is `"vector"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument
for it. Overture is a static per-release snapshot, so `start` / `end`
are accepted but ignored (there is no temporal axis to iterate).

Overture needs **no credentials** — the bucket is public, so (like
GDACS / CHC) there is no auth class; the only extra is the
`overturemaps` SDK (`pip install earthlens[overture]`).

Theme/type selection: for this backend `variables` is a
`dict[str, list[str]]` mapping a friendly theme name to its requested
feature types — `variables={"buildings": []}` (the theme's primary
type), `variables={"places": ["place"]}`, or several at once. An empty
type list defaults to the theme's `default_type`.

The headline feature is **per-row license provenance**: Overture's
`sources` column records each feature's upstream datasets and their
licenses; the backend derives a per-feature `license_id` column and
warns (with a `LicenseWarning`) when any `ODbL-1.0` (OSM-derived) rows
are present — critical for downstream commercial users.

Public surface (re-exported from this package):

* `Overture` — the backend; instantiate with a bbox and
  `variables={theme: [type, ...]}`, then call `Overture.download`.
* `Catalog` — pydantic-backed loader for the bundled
  `overture_data_catalog.yaml` theme/type dispatch table.
* `Theme` — one theme's dispatch row (`types`, `default_type`,
  `geometry`, `key_columns`, `licenses`).
* `LicenseWarning` — emitted when ODbL-1.0 rows are present in a result.
* `to_feature_collection` / `empty_fc` — the Overture `GeoDataFrame` →
  FeatureCollection mapper (which adds `license_id`) and its
  empty-result counterpart.
* `CATALOG_PATH` — path to the bundled theme YAML.

Examples:
    - List the curated themes:

        ```python
        >>> from earthlens.overture import Catalog
        >>> Catalog().themes()
        ['addresses', 'base', 'buildings', 'divisions', 'places', 'transportation']

        ```
"""

from __future__ import annotations

from earthlens.overture._helpers import LicenseWarning, row_license
from earthlens.overture.backend import Overture
from earthlens.overture.catalog import CATALOG_PATH, Catalog, Theme
from earthlens.overture.collection import empty_fc, to_feature_collection

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "LicenseWarning",
    "Overture",
    "Theme",
    "empty_fc",
    "row_license",
    "to_feature_collection",
]
