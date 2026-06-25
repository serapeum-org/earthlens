"""Shared helpers for the biodiversity cluster (`gbif` / `obis` / `wdpa` / `iucn`).

The four biodiversity backends are independent provider subpackages, but they
share a small request/output shape: a bounding box becomes a WKT polygon, a
batch of occurrence records becomes a points `FeatureCollection`, and a result
that carries licensing obligations raises a `LicenseWarning`. Those three
pieces live here so each backend stays thin.

This package is **not** a data source — it registers no facade key and ships no
catalog. It is a helper module the cluster backends import from:

* `wkt_from_bbox` — a `SpatialExtent` (which exposes `.west/.south/.east/.north`
  but no `.wkt()`) to a counter-clockwise `POLYGON((...))` WKT string the
  occurrence/area APIs accept as their `geometry=` filter.
* `occurrences_to_fc` — occurrence rows (a `list[dict]` from `pygbif`, or the
  `pandas.DataFrame` `pyobis` returns) to a `pyramids` `FeatureCollection` of
  `EPSG:4326` points, modelled on the shipped `fdsn.events` mapper.
* `LicenseWarning` / `warn_license` — the shared per-result license guard,
  promoted here from the Overture backend so every cluster source can flag its
  attribution / non-commercial / redistribution obligations the same way.
"""

from __future__ import annotations

from earthlens.biodiversity._helpers import (
    IUCN_LICENSE,
    RESTRICTIVE_LICENSES,
    WDPA_LICENSE,
    LicenseWarning,
    occurrences_to_fc,
    parse_retry_after,
    warn_license,
    wkt_from_bbox,
)

__all__ = [
    "IUCN_LICENSE",
    "LicenseWarning",
    "RESTRICTIVE_LICENSES",
    "WDPA_LICENSE",
    "occurrences_to_fc",
    "parse_retry_after",
    "warn_license",
    "wkt_from_bbox",
]
