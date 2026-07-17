# Protected Planet (WDPA) — introduction

<img src="../../_images/logos/wdpa.png" alt="Protected Planet (UNEP-WCMC) logo" height="60">

[Protected Planet](https://www.protectedplanet.net/) publishes the **World
Database on Protected and Conserved Areas (WDPCA)** — 300 k+ protected and
conserved areas worldwide, with their boundaries, designations, and IUCN
management categories. earthlens ships a `wdpa` backend (alias
`protected-planet`) that fetches protected-area **polygons** from the
Protected Planet **v4** REST API.

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](wdpa.md) page.

## Why a direct v4 client

The `pywdpa` package is **not** used: it is pinned to the **v3** API — which
Protected Planet takes down on **2026-05-01** — and writes an ESRI shapefile
to disk rather than returning an in-memory frame. Instead the backend talks to
`https://api.protectedplanet.net/v4` directly with `requests` (a core
dependency, so there is no extra to install). It is the cluster's only
polygon-geometry backend.

## Authentication

v4 requires a personal token, passed as a **`?token=` query parameter** (not
an `Authorization: Bearer` header). Resolve it from a `token=` argument or the
`WDPA_TOKEN` environment variable; a missing token raises
`AuthenticationError` naming `WDPA_TOKEN`. Request a token at
[api.protectedplanet.net/request](https://api.protectedplanet.net/request).

## What it returns

`wdpa` is a `vector` backend (`WDPA.OUTPUT_KIND == "vector"`): each entry in
`variables` selects a **country** (ISO3 code, `"KEN"` or `"country:KEN"`) or a
single **WDPA id** (`"555"`). The backend pages
`protected_areas/search?country=…&with_geometry=true` (50 areas per page) — or
fetches `protected_areas/<id>` — parses each area's embedded GeoJSON, drops
point-only areas, and returns the polygons as a
[pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection` in
`EPSG:4326`. The facade rejects an `aggregate=` argument.

## Licensing

WDPA data carries a **custom UNEP-WCMC license**, not Creative Commons:
commercial use needs prior written permission, redistribution is restricted,
and a specific citation is required. **Every** download raises a
`LicenseWarning` so the obligation is never silent. See the
[Protected Planet legal terms](https://www.protectedplanet.net/en/legal).
