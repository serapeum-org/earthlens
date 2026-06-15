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

A plain operational request **downloads the whole-CONUS files** (~14 MB
`channel_rt`, ~30 MB `land`). A **subset** — `sites=` (`feature_id` /
USGS `gage_id`), a narrower bbox, or a `[start, end]` window — and the
**retrospective** archive (`mode="retrospective"`) are read through
pyramids (≥ 0.34.0): the feature/lake/node-indexed **tabular** products
(`chrtout`, `lakeout`, `coastal`) go through
`pyramids.netcdf.LabeledDataset` (open anon + lazily, select
labels/bbox/time, write a tidy `feature_id × time` Parquet table); the
**gridded** products (`ldasout`, `rtout`, `forcing`) go through
`pyramids.netcdf.NetCDF.subset` (an operational bbox crop on the native
grid → GeoTIFF). earthlens never imports `xarray` / `zarr` itself —
pyramids owns the read. The gridded **retrospective** (and a variable
with an interleaved vertical/layer dimension, e.g. `SOIL_M`) is deferred
with a clear `NotImplementedError`.

The `[nwm]` extra pulls `boto3` (unsigned S3) plus
`pyramids-gis[parquet]` (the pyramids readers); both are
imported lazily, so the package imports — and `NWM(...)` constructs —
without the extra installed (a friendly `ImportError` naming
`earthlens[nwm]` surfaces at `download()` time).

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
