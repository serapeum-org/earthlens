"""Humanitarian Data Exchange backend (CKAN via hdx-python-api).

One unified backend over UN OCHA's Humanitarian Data Exchange
(`data.humdata.org`, ~41k datasets): a single read-only
`hdx-python-api` client resolves a curated dataset (or an arbitrary HDX
id), filters its resources, and downloads the matching files to disk.
HDX is a public catalogue, so there is no authentication.

Unlike every gridded earthlens backend, the output shape is the fixed
value `"mixed"` — :class:`HDX` is the first mixed backend: one dataset
can carry raster, vector and tabular resources at once. The MVP
downloads each resource file in its native format and records its CKAN
format label; reading / converting it into a pyramids type is the
deferred `PY-D` work item.

Public surface (re-exported from this package):

* :class:`HDX` — the backend itself; instantiate with a
  `{dataset_key: [resource_filter, ...]}` mapping (or the `hdx_id=`
  escape hatch), then call :meth:`HDX.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled per-theme
  `catalog/` directory.
* :class:`HdxDataset` — one curated dataset row (`hdx_id`, `org`,
  `title`, `themes`, `formats`, `resource_filter`, `output_kinds`).
* :data:`CATALOG_PATH` — absolute path to the bundled `catalog/`
  directory; monkey-patchable to redirect the loader.

The `[hdx]` extra pulls `hdx-python-api>=6,<7`. The import is lazy, so
this package imports without the extra installed; a missing extra
surfaces as a friendly `ImportError` naming `earthlens[hdx]`.
"""

from __future__ import annotations

from earthlens.hdx.backend import HDX
from earthlens.hdx.catalog import CATALOG_PATH, Catalog, HdxDataset

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "HDX",
    "HdxDataset",
]
