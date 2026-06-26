"""NREL solar / wind resource time-series backend.

Direct, keyed REST client over the NREL/NLR Developer Network CSV download API
(`https://developer.nlr.gov/api/...`). A request selects a product via the
`product=` kwarg — the NSRDB GOES Aggregated PSM v4 hourly solar series
(`"nsrdb-psm3"`), the NSRDB GOES TMY v4 series (`"nsrdb-tmy"`), or the WIND
Toolkit hourly wind series (`"wtk"`) — picks variables with
`variables=["ghi", "dni", ...]`, samples the location(s) (a single point or a
bbox expanded to a point grid), issues one throttled keyed `GET` per
`(point, year)` (≤ 1 req/s, 5000/day), parses each CSV into a long-format
`pandas.DataFrame` tagged with `lat`/`lon`/`year`/`product`, and concatenates
them.

This is a `tabular` backend: the result is a per-coordinate time-series table,
not a gridded array, so `NREL.OUTPUT_KIND` is `"tabular"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument for it
(NREL already returns the resolved series). There is no heavy gridded-archive
SDK and no `pyramids` array dependency — pure `requests` + `pandas`; no array /
NetCDF / HSDS layer.

Authentication is **required**: a free NREL API key *and* the email that
registered it, passed as `api_key=` / `email=` or via `NREL_API_KEY` /
`NREL_EMAIL`.

Public surface (re-exported from this package):

* `NREL` — the backend; instantiate with a date range, a point (or bbox),
  `variables=[...]`, and credentials, then call `NREL.download`.
* `Catalog` / `Product` — pydantic-backed loader for the bundled
  `nrel_data_catalog.yaml` product table and one product's row.
* `NrelAuth` / `NrelCredentials` / `AuthenticationError` — the required
  two-secret (key + email) auth surface.
* `CATALOG_PATH` — path to the bundled product YAML.
"""

from __future__ import annotations

from earthlens.nrel.auth import (
    AuthenticationError,
    NrelAuth,
    NrelCredentials,
)
from earthlens.nrel.catalog import (
    CATALOG_PATH,
    Catalog,
    Product,
    clear_catalog_cache,
)

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "NrelAuth",
    "NrelCredentials",
    "Product",
    "clear_catalog_cache",
]
