"""JAXA backend — Earth-observation archive over three protocols.

`earthlens.jaxa` reaches JAXA's Earth-observation catalogue via three
complementary SDKs, selected per-dataset by a `protocol` discriminator:

* `protocol: jaxa-earth` — authless STAC + COG access through the official
  `jaxa.earth` API (AW3D30 elevation, GSMaP precipitation, AMSR2 L3
  re-hosts, JASMES MODIS re-hosts, …). The API returns in-memory numpy
  arrays which the backend writes to north-up GeoTIFFs via
  `pyramids.dataset.Dataset.from_array`.
* `protocol: gportal` — credentialed SFTP access through the community
  `gportal` SDK (raw L1/L2 swaths of GCOM-W AMSR2 / GCOM-C SGLI /
  ALOS-2 PALSAR-2 / EarthCARE / GPM / …). Requires a free G-Portal
  account; `JaxaAuth(creds, protocol="gportal").configure()` resolves
  the credentials from explicit kwargs or `$GPORTAL_USERNAME` /
  `$GPORTAL_PASSWORD`.
* `protocol: ptree` — credentialed plain-FTP access to
  `ftp.ptree.jaxa.jp` for near-real-time Himawari-8/9 AHI HSD granules
  (30-day rolling archive, 10-min cadence, 10 segments per band per
  slot). Uses **stdlib `ftplib` only** — no additional dependency.
  Requires a free P-Tree account (separate from G-Portal);
  `JaxaAuth(creds, protocol="ptree").configure()` resolves the
  credentials from explicit kwargs or `$JAXA_PTREE_USERNAME` /
  `$JAXA_PTREE_PASSWORD`. Ships raw `.DAT.bz2` granules only — decoding
  HSD to arrays is `satpy`'s job (tracked as pyramids `PY-2`).

Importing this subpackage does **not** require the `[jaxa]` extra —
`jaxa.earth` and `gportal` are imported inside their branch modules
lazily, and the `ptree` branch uses only stdlib. Install
`pip install 'earthlens[jaxa]'` to enable the two SDK-backed branches.
"""

from __future__ import annotations

from earthlens.jaxa._ptree import RetentionError
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
    "RetentionError",
    "Catalog",
    "Dataset",
    "CATALOG_PATH",
    "clear_catalog_cache",
]
