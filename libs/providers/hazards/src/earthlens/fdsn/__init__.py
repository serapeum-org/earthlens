"""FDSN seismic-event backend.

Thin wrapper over `obspy.clients.fdsn` that queries the IRIS
FDSN-event web service across six seismological networks — USGS
(ComCat), EMSC (seismicportal), INGV (Italian seismic + volcano),
EarthScope (ex-IRIS DMC), ISC (global reviewed bulletin), and GeoNet
(New Zealand) — and returns the matched events as a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of point
features (CRS `EPSG:4326`).

This is the package's first `vector` backend: the result is a table of
events, not a gridded array, so :data:`FDSN.OUTPUT_KIND` is
`"vector"` and the :class:`earthlens.earthlens.core.EarthLens` facade
rejects an `aggregate=` argument for it.

Provider selection: for this backend `variables` is a `list[str]` of
network keys — `variables=["USGS"]`, `variables=["USGS", "EMSC"]` —
**not** data-variable names. This is an intentional, documented
overload (the facade makes `variables` a required argument, so an
extra `providers=` kwarg would only add placeholder noise). Query
filters (`min_magnitude`, `max_depth`, `event_type`, …) arrive as
explicit :class:`FDSN` constructor keyword arguments.

Public surface (re-exported from this package):

* :class:`FDSN` — the backend; instantiate with a date range, a bbox,
  and `variables=[network, ...]`, then call :meth:`FDSN.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `fdsn_data_catalog.yaml` provider dispatch table.
* :class:`Provider` — one network's dispatch row (`fdsn_id`, title,
  `needs_token`, default min-magnitude, docs URL).
* :func:`catalog_to_fc` / :func:`empty_fc` — the `obspy.Catalog` →
  FeatureCollection mapper and its empty-result counterpart.
* :func:`resolve_earthscope_token` — optional EarthScope-token
  resolver (env / file); the public event services need no token.
* :data:`CATALOG_PATH` — path to the bundled provider YAML.

Examples:
    - List the registered networks:

        ```python
        >>> from earthlens.fdsn import Catalog
        >>> sorted(Catalog().providers)
        ['EARTHSCOPE', 'EMSC', 'GEONET', 'INGV', 'ISC', 'USGS']

        ```
"""

from __future__ import annotations

from earthlens.fdsn.auth import resolve_earthscope_token
from earthlens.fdsn.backend import FDSN
from earthlens.fdsn.catalog import CATALOG_PATH, Catalog, Provider
from earthlens.fdsn.events import catalog_to_fc, empty_fc

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "FDSN",
    "Provider",
    "catalog_to_fc",
    "empty_fc",
    "resolve_earthscope_token",
]
