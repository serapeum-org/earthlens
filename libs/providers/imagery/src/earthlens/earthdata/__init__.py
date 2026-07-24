"""NASA Earthdata backend (EOSDIS via earthaccess + CMR).

One unified backend over the NASA EOSDIS archive: a single Earthdata
Login (EDL) authenticates once and reaches the user-relevant DAACs
(PO.DAAC, NSIDC, LP DAAC, OB.DAAC, GES DISC, LAADS, ASF, ORNL, ASDC)
through `earthaccess` + CMR. The MVP fetches whole native granules to
disk (HTTPS `download`, or in-region S3 `open`); server-side subsetting
(Harmony) and ASF's richer search are deferred.

Unlike every other earthlens backend, the output shape is
**per-dataset, not fixed** — :class:`EarthData` sets `OUTPUT_KIND` from
the resolved catalog row (`raster` / `vector` / `tabular`).

Public surface (re-exported from this package):

* :class:`EarthData` — the backend itself; instantiate with a date
  range, a bbox, and a `{dataset_key: [band, ...]}` mapping, then call
  :meth:`EarthData.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled per-DAAC
  `catalog/` directory.
* :class:`EarthdataDataset` — one curated dataset row (short_name,
  version, provider, output_kind, format, cloud-hosting flags, bands).
* :class:`Band` — one band's metadata row (informational).
* :class:`EarthdataAuth` — `AbstractAuth` wrapper over
  `earthaccess.login`. Idempotent; safe to call repeatedly.
* :class:`EarthdataCredentials` — frozen value object the auth class
  binds to.
* :class:`AuthenticationError` — raised when EDL login fails; subclass
  of :class:`earthlens.base.AuthenticationError`.
* :data:`CATALOG_PATH` — absolute path to the bundled `catalog/`
  directory; monkey-patchable to redirect the loader.

The `[earthdata]` extra pulls `earthaccess>=0.18`, which requires
**Python ≥3.12** even though earthlens core targets ≥3.11. The
`earthaccess` import is lazy, so this package imports without the extra
installed.
"""

from __future__ import annotations

from earthlens.earthdata.auth import (
    AuthenticationError,
    EarthdataAuth,
    EarthdataCredentials,
)
from earthlens.earthdata.backend import EarthData, Earthdata
from earthlens.earthdata.catalog import (
    CATALOG_PATH,
    PROVIDERS_PATH,
    Band,
    Catalog,
    EarthdataDAAC,
    EarthdataDataset,
)

__all__ = [
    "AuthenticationError",
    "Band",
    "CATALOG_PATH",
    "PROVIDERS_PATH",
    "Catalog",
    "EarthData",
    "Earthdata",
    "EarthdataAuth",
    "EarthdataCredentials",
    "EarthdataDAAC",
    "EarthdataDataset",
]
