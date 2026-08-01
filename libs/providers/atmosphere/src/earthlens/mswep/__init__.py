"""GloH2O MSWEP / MSWX backend (merged precipitation + meteorological forcing).

Downloads raw **MSWEP** (multi-source weighted-ensemble precipitation,
hourly 0.1°, 1979→~2 h from real-time) and **MSWX** (the companion
bias-corrected meteorological forcing, 3-hourly 0.1°, 10 variables)
NetCDF granules from the Google-Drive folder GloH2O shares with an
approved non-commercial user. `OUTPUT_KIND = "raster"`; `download()`
returns the `list[Path]` of granules written. Reading and regridding the
NetCDF is **pyramids**' job — this package never imports `xarray`.

**Access is a prerequisite, not something this backend can arrange.**
There is no anonymous download: a user submits a request form per
product (`gloh2o.org/mswep`, `gloh2o.org/mswx`), and on approval GloH2O
shares a Drive folder with **their own Google account** plus `rclone`
instructions. earthlens automates *their* approved download. The
credential must therefore be a **user** OAuth token — a service account
is a separate principal that cannot see the share (see
:mod:`earthlens.mswep.auth`).

Bulk transfers stay `rclone`'s job: an hourly year is ~8760 granules.
This backend is for targeted product / variant / resolution / window
requests.

Public surface (re-exported from this package):

* :class:`MSWEP` — the backend; instantiate with a date window, a
  product / variant / resolution and the shared-folder id, then call
  :meth:`MSWEP.download`.
* :class:`MswepAuth` / :class:`MswepCredentials` — the Drive credential
  ladder (explicit `token.json` → `rclone` remote → environment) and the
  shared-folder id.
* :class:`AuthenticationError` — raised when no usable credential or
  folder id resolves; a subclass of
  :class:`earthlens.base.AuthenticationError`.
* :data:`DRIVE_SCOPE` — the read-only Drive scope the backend requests.
* :class:`Catalog` — loader for the bundled `mswep_data_catalog.yaml`.
* :class:`MswepProduct` / :class:`MswepVersion` / :class:`MswepVariant` /
  :class:`MswepResolution` / :class:`MswepVariable` — one product row and
  its four coordinate maps.
* :class:`ProvisionalValueError` — raised when a request resolves onto a
  catalog value that could not be verified without an approved share.
* :data:`CATALOG_PATH` — path to the bundled YAML; monkey-patchable in
  tests.

Examples:
    - Construct credentials that resolve entirely from the environment:
        ```python
        >>> from earthlens.mswep import MswepCredentials
        >>> MswepCredentials().folder_id is None
        True

        ```
    - The two products carry different Drive path shapes:
        ```python
        >>> from earthlens.mswep import Catalog
        >>> Catalog().products()
        ['mswep', 'mswx']

        ```
"""

from __future__ import annotations

from earthlens.mswep.auth import (
    DRIVE_SCOPE,
    AuthenticationError,
    MswepAuth,
    MswepCredentials,
)
from earthlens.mswep.backend import CADENCES, MSWEP
from earthlens.mswep.catalog import (
    CATALOG_PATH,
    Catalog,
    MswepProduct,
    MswepResolution,
    MswepVariable,
    MswepVariant,
    MswepVersion,
    ProvisionalValueError,
    clear_catalog_cache,
)
from earthlens.mswep.drive import (
    DownloadQuotaExceededError,
    DriveTransportError,
    RateLimitedError,
)

__all__ = [
    "AuthenticationError",
    "CADENCES",
    "CATALOG_PATH",
    "Catalog",
    "DRIVE_SCOPE",
    "DownloadQuotaExceededError",
    "DriveTransportError",
    "MSWEP",
    "MswepAuth",
    "MswepCredentials",
    "MswepProduct",
    "MswepResolution",
    "MswepVariable",
    "MswepVariant",
    "MswepVersion",
    "ProvisionalValueError",
    "RateLimitedError",
    "clear_catalog_cache",
]
