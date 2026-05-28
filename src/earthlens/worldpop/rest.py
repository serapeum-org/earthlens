"""WorldPop REST query layer.

Thin `requests`-based client over `hub.worldpop.org/rest/data`. The hub's
three-level scheme is: `/rest/data` (product aliases) →
`/rest/data/{alias}` (sub-aliases) → `/rest/data/{alias}/{subalias}?iso3=…`
(one JSON record per year, each carrying a `files` array of GeoTIFF URLs).
Year filtering is **client-side on `popyear`** — the query returns every
year. The query helpers land in `C3`; this module pins the base URL.
"""

from __future__ import annotations

#: Base URL of the WorldPop REST data catalogue (no auth; CC-BY-4.0).
BASE_URL: str = "https://hub.worldpop.org/rest/data"
