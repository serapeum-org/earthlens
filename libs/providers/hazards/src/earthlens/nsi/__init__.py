"""US object-level flood exposure & loss backend (`earthlens.nsi`).

One backend over three keyless US-federal REST sources, selected by a `source=`
discriminator:

* **`structures`** (default) — **USACE National Structure Inventory (NSI)**:
  building points with occupancy, replacement value, foundation type/height, and
  area; `vector`.
* **`nfhl`** — **FEMA National Flood Hazard Layer**: regulatory flood zones
  (`FLD_ZONE`, `SFHA_TF`) from the ArcGIS `S_Fld_Haz_Ar` layer; `vector`.
* **`nfip`** — **FEMA NFIP redacted claims (v3)**: flood-insurance claim records
  with paid amounts, via the OpenFEMA OData endpoint; `tabular`.

Output is **per instance**: the resolved source's `output_kind` decides whether
:meth:`~earthlens.nsi.backend.NSI.download` returns a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` (`structures`/`nfhl`) or
a :class:`pandas.DataFrame` (`nfip`). All three are public-domain and keyless —
no auth. A spatial/attribute bound is **required** (no unbounded national pull),
and `aggregate=` is rejected (these are records, not gridded rasters).

The public surface is the :class:`Catalog` (source name -> endpoint + output kind
+ field map) and its :class:`Source` rows, the :class:`NSI` backend, and the pure
query/geometry helpers.
"""

from __future__ import annotations

from earthlens.nsi.backend import NSI
from earthlens.nsi.catalog import Catalog, Source, clear_catalog_cache
from earthlens.nsi.geometry import (
    arcgis_envelope,
    bbox_from_limits,
    nsi_polygon_body,
    to_feature_collection,
)

__all__ = [
    "NSI",
    "Catalog",
    "Source",
    "clear_catalog_cache",
    "to_feature_collection",
    "nsi_polygon_body",
    "arcgis_envelope",
    "bbox_from_limits",
]
