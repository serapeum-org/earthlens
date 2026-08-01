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

* :class:`MswepAuth` / :class:`MswepCredentials` — the Drive credential
  ladder (explicit `token.json` → `rclone` remote → environment) and the
  shared-folder id.
* :class:`AuthenticationError` — raised when no usable credential or
  folder id resolves; a subclass of
  :class:`earthlens.base.AuthenticationError`.
* :data:`DRIVE_SCOPE` — the read-only Drive scope the backend requests.

Examples:
    - Construct credentials that resolve entirely from the environment:
        ```python
        >>> from earthlens.mswep import MswepCredentials
        >>> MswepCredentials().folder_id is None
        True

        ```
"""

from __future__ import annotations

from earthlens.mswep.auth import (
    DRIVE_SCOPE,
    AuthenticationError,
    MswepAuth,
    MswepCredentials,
)

__all__ = [
    "AuthenticationError",
    "DRIVE_SCOPE",
    "MswepAuth",
    "MswepCredentials",
]
