"""JAXA backend — Earth-observation archive over two protocols.

`earthlens.jaxa` reaches JAXA's Earth-observation catalogue via two
complementary SDKs, selected per-dataset by a `protocol` discriminator:

* `protocol: jaxa-earth` — authless STAC + COG access through the official
  `jaxa.earth` API (AW3D30 elevation, GSMaP precipitation, AMSR2 L3
  re-hosts, JASMES MODIS re-hosts, …). The API returns in-memory numpy
  arrays which the backend writes to north-up GeoTIFFs via
  `pyramids.dataset.Dataset.create_from_array`.
* `protocol: gportal` — credentialed SFTP access through the community
  `gportal` SDK (raw L1/L2 swaths of GCOM-W AMSR2 / GCOM-C SGLI /
  ALOS-2 PALSAR-2 / EarthCARE / GPM / …). Requires a free G-Portal
  account; `JaxaAuth.configure("gportal")` resolves the credentials from
  explicit kwargs or `$GPORTAL_USERNAME` / `$GPORTAL_PASSWORD`.

The third JAXA archive, **P-Tree** (Himawari geostationary AHI, FTP +
HSD `.bz2`), is a deferred follow-on (planning `G8`).

Importing this subpackage does **not** require the `[jaxa]` extra — the
two SDK imports happen inside the branch modules and are skipped when
the request resolves to the other protocol. Install
`pip install 'earthlens[jaxa]'` to enable both branches.
"""

from __future__ import annotations

from earthlens.jaxa.auth import (
    AuthenticationError,
    JaxaAuth,
    JaxaCredentials,
    JaxaProtocol,
)
from earthlens.jaxa.backend import JAXA
from earthlens.jaxa.catalog import CATALOG_PATH, Catalog, Dataset, clear_catalog_cache

__all__ = [
    "JAXA",
    "JaxaAuth",
    "JaxaCredentials",
    "JaxaProtocol",
    "AuthenticationError",
    "Catalog",
    "Dataset",
    "CATALOG_PATH",
    "clear_catalog_cache",
]
