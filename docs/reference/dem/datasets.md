# Copernicus DEM — available datasets

The `dem` backend curates two open Copernicus DEM grids from the ESA /
Airbus TanDEM-X derived collection. Both live on anonymous AWS Open Data
buckets in `eu-central-1`, both cover the whole global land surface as
1° x 1° COG tiles, and both need no credentials.

| `dataset=` | Bucket | Native res. | Tile-name token | Datum |
|---|---|---|---|---|
| `cop-dem-glo-30` (default) | `copernicus-dem-30m` | ~30 m | `10` | EGM2008 vertical, WGS84 horizontal |
| `cop-dem-glo-90` | `copernicus-dem-90m` | ~90 m | `30` | EGM2008 vertical, WGS84 horizontal |

## Discoverability aliases

The four keys `dem`, `copernicus-dem`, `cop-dem`, and `dem:elevation` all
route to the same backend — pick whichever reads best in your code:

```python
EarthLens(data_source="dem", ...)
EarthLens(data_source="copernicus-dem", ...)
EarthLens(data_source="cop-dem", ...)
EarthLens(data_source="dem:elevation", ...)
```

## Coverage

Both grids are **global land**, tiled on the integer-degree grid. Ocean
tiles are absent from the bucket (see
[Introduction](introduction.md#how-it-works)). A bbox that lies entirely
on land returns one COG per intersected tile; a coastal bbox returns
only the land tiles and logs each ocean-side miss.

## Attribution

Both grids are © ESA, produced from Copernicus data, and free to use
with attribution.

## What is not shipped in the first cut

- **OpenTopography's `globaldem` REST** (SRTM / NASADEM / AW3D30 / USGS
  3DEP / ArcticDEM / REMA behind a single endpoint) is intentionally
  **deferred** — it needs an API key + a per-day rate limit, which
  breaks the account-free premise this backend is built for. It may be
  added later as a second endpoint, but Copernicus DEM stays the
  default.
- **JAXA AW3D30** and **FABDEM** — plausible drop-in additions if a
  user needs them; not shipped in the first cut.
- **ArcticDEM / REMA mosaics** — need their own tile-strip-versioned
  handling; out of scope for a bbox-tile backend.
