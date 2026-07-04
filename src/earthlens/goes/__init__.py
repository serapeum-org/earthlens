"""NOAA GOES-R series ABI backend (raw NetCDF granules from public S3).

Fetches GOES-R Advanced Baseline Imager (ABI) imagery from the public,
anonymous AWS Open-Data buckets `noaa-goes19` (operational GOES-East),
`noaa-goes18` (GOES-West), and `noaa-goes16` (standby / archive). It is a
**raster** backend: :meth:`GOES.download` returns the `list[Path]` of raw
**NetCDF** ABI granules whose scan-start time falls in the requested
window. It does **not** decode them — reading / reprojecting the
geostationary NetCDF to arrays is pyramids' (or `satpy`'s) job downstream,
so `earthlens.goes` never imports `xarray` / `netCDF4` / `goes2go`.

A request is three-axis: a **satellite** (`satellite="east"` /
`"west"` / `"16"` / `"18"` / `"19"` — roles resolve to the current
operational bucket), a **product** (`dataset="abi-l2-mcmip"`, an ABI
product family), and a **domain** (`domain="C"` CONUS / `"F"` Full Disk /
`"M1"` / `"M2"` Mesoscale). The backend enumerates the
`<Product>/<YYYY>/<DDD>/<HH>/` S3 prefixes across the window, keeps the
granules whose `_s<scan-start>` time lands in `[start, end]`, and
downloads them.

GOES rides the shipped **`[s3]`** extra (unsigned `boto3`) — no new SDK,
no auth (`goes = ["earthlens[s3]"]`). The client is imported lazily, so
the package imports — and `GOES(...)` constructs — without the extra
installed (a friendly `ImportError` naming `earthlens[s3]` surfaces at
`download()` time). GOES data is US Government public domain: no licence
gate, attribution only in the docs.

Public surface (re-exported from this package):

* :class:`GOES` — the backend; instantiate with a scan-time window, a
  bbox, `dataset=` / `variables=`, `satellite=`, and `domain=`, then call
  :meth:`GOES.download`.
* :class:`Catalog` — loader for the bundled `goes_data_catalog.yaml`.
* :class:`GOESProduct` / :class:`GOESDomain` / :class:`GOESChannel` — one
  product row, one scan domain, and one ABI spectral band.
* :data:`CATALOG_PATH` — path to the bundled YAML; monkey-patchable in
  tests.

Examples:
    - List the registered GOES products:
        ```python
        >>> from earthlens.goes import Catalog
        >>> "abi-l2-mcmip" in Catalog().products()
        True

        ```
"""

from __future__ import annotations

from earthlens.goes.catalog import (
    CATALOG_PATH,
    Catalog,
    GOESChannel,
    GOESDomain,
    GOESProduct,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "GOESChannel",
    "GOESDomain",
    "GOESProduct",
]
