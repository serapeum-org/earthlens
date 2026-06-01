# GHSL — Projections, CRS & tiling

GHSL is published in two coordinate systems and two delivery layouts. The
backend hides the details, but understanding them helps you choose
`resolution` / `crs` / `tiling`.

## Source CRS: Mollweide vs WGS84

| Resolution | Unit | Source CRS |
|---|---|---|
| `10m`, `100m`, `1km` | metres | **Mollweide** (ESRI:54009) — equal-area |
| `3ss`, `30ss` | arc-seconds | **WGS84** (EPSG:4326) |

JRC distributes the equal-area metric grids in Mollweide (the projection of
record for GHSL — it preserves area, which matters for counting people and
summing built-up surface) and the geographic grids in WGS84. **The resolution
you pick determines the source CRS** — there is no 100 m WGS84 file, nor a 30″
Mollweide file.

## Output CRS & when reprojection happens

`crs=` is the **output** CRS (default `EPSG:4326`). The backend reprojects the
downloaded source to it via `pyramids`:

* Source 54009 + output 4326 → **reprojected** (the common case for the 100 m
  grids).
* Source 4326 + output 4326 → **no reprojection** (the 3″/30″ grids are served
  in WGS84 already).
* Any other output (e.g. `crs="EPSG:3035"`) → reprojected.

Continuous products reproject with **bilinear** resampling; categorical
products (SMOD, BUILT-C, DEGURBA) use **nearest-neighbour** so class codes are
never blended into non-existent values at tile seams.

## Tiling: tiles vs whole-globe

| Layout | Resolutions | Delivery |
|---|---|---|
| **Tiled** | `10m`, `100m`, `3ss` | a sparse 18×36 Mollweide grid of ~1000 km tiles (`R{row}_C{col}`), land only |
| **Whole-globe** | `1km`, `30ss` | a single global `.zip` (~300 MB) |

For a tiled product the backend transforms your AOI to Mollweide (densifying
the bbox edges so the bowed Mollweide image is captured), intersects it with
the bundled 375-tile land schema (`tile_schema.geojson`), downloads only the
intersecting tiles, and mosaics them. `tiling="global"` overrides this to fetch
the whole-globe file instead (useful when you want the full grid or your AOI
spans many tiles).

If a tiled-product AOI intersects **no** land tile (open ocean), the backend
raises a clear error rather than producing an empty raster — switch to a land
AOI, a coarse resolution, or `tiling="global"`.

## The tile grid

The grid is the official JRC `GHSL_data_54009_shapefile` (the
`GHSL2_0_MWD_L1_tile_schema_land` layer), converted to a compact GeoJSON and
shipped as package data. It is a fixed 18×36 scheme (`R1..R18` north→south,
`C1..C36` west→east) of exactly 1,000,000 m square tiles; only the 375 tiles
that contain land exist. Regenerate it with:

```bash
python tools/ghsl/refresh_ghsl_catalog.py refresh-tiles
```
