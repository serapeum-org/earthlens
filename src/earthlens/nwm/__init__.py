"""NOAA National Water Model backend (operational hydrologic output).

Fetches National Water Model v3.0 NetCDF output from the unsigned
`noaa-nwm-pds` AWS bucket. NWM routes the land-surface water budget onto
the NHDPlus v2 river network: `chrtout` is per-reach streamflow (indexed
by `feature_id`, **not** a lat/lon grid — so `OUTPUT_KIND = "tabular"`),
alongside the gridded `ldasout` land-surface product
(`OUTPUT_KIND = "raster"`). The request is two-axis: `variables =
{product: [variable, ...]}` picks the products and `configuration=`
picks the operational run (`short_range`, `analysis_assim`,
`medium_range`), which runs on UTC `cycles` and publishes forecast
(`fNNN`) / analysis (`tmNN`) `steps`.

This is a **whole-CONUS download** backend: operational files (~14 MB
`channel_rt`, ~30 MB `land`) are fetched whole. Any subset — `sites=` /
`feature_id` / a narrower bbox, or the retrospective Zarr
(`mode="retrospective"`) — needs a read, which is the pyramids `PY-G`
capability (unreleased), so it raises a clear `NotImplementedError`
naming `PY-G`. earthlens never imports `xarray` / `zarr`.

The `[nwm]` extra pulls `boto3` (unsigned S3); it is imported lazily, so
the package imports — and `NWM(...)` constructs — without the extra
installed (the `ImportError` naming `earthlens[nwm]` surfaces at
`download()` time).

Public surface (re-exported from this package):

* :class:`NWM` — the backend; instantiate with a cycle-date window, a
  bbox, `variables={product: [variable, ...]}`, and a `configuration=`,
  then call :meth:`NWM.download`.
* :class:`Catalog` — loader for the bundled `nwm_data_catalog.yaml`.
* :class:`NWMProduct` / :class:`NWMVariable` / :class:`NWMConfig` — one
  product row, one of its variables, and one configuration row.
* :data:`CATALOG_PATH` — path to the bundled YAML; monkey-patchable in
  tests.
* :data:`BUCKET` — the unsigned NWM bucket name.

Examples:
    - List the registered NWM products:
        ```python
        >>> from earthlens.nwm import Catalog
        >>> Catalog().products()
        ['chrtout', 'coastal', 'forcing', 'lakeout', 'ldasout', 'rtout']

        ```
"""

from __future__ import annotations

from earthlens.nwm.backend import BUCKET, NWM
from earthlens.nwm.catalog import (
    CATALOG_PATH,
    Catalog,
    NWMConfig,
    NWMProduct,
    NWMVariable,
)

__all__ = [
    "BUCKET",
    "CATALOG_PATH",
    "Catalog",
    "NWM",
    "NWMConfig",
    "NWMProduct",
    "NWMVariable",
]
