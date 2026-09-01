# GHSL — Usage

## Request shape

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="ghsl",              # or "ghs"; topic key "ghsl:human-settlement"
    variables=["GHS_POP"],           # product keys or friendly aliases
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[40.3, 40.5],            # [south, north] degrees
    lon_lim=[-3.8, -3.6],            # [west, east] degrees
    path="out/",
    # --- GHSL-specific kwargs (all optional) ---
    release=None,                    # None = auto-resolve per product; or an explicit id
    resolution="100m",               # "10m" / "100m" / "1km" / "3ss" / "30ss"
    crs="EPSG:4326",                 # output CRS (default WGS84)
    tiling="auto",                   # "auto" (tile+mosaic) / "global"
    api="direct",                    # "direct" (default); "stac" is unavailable
    # epoch=2020,  epochs=[2000, 2020],
).download(progress_bar=True)        # -> [Path, ...] one GeoTIFF per (product, epoch)
```

`variables` is a **list of GHSL product keys** — canonical (`"GHS_POP"`,
`"GHS_SMOD"`, `"GHS_BUILT_H_ANBH"`) or friendly aliases (`"population"`,
`"settlement_model"`, `"built_height"`, …). Pass several to fetch several
products in one call.

## The kwargs

| kwarg | meaning |
|---|---|
| `release` | GHSL release, or `None` (default) to auto-resolve per product: `"R2023A"` when offered, else the product's single release (so `GHS_LAND` (R2022A), the R2019A/R2024A statistical families, and the R2025A WUP family need no explicit release; a request may even mix releases). An **explicit** release must be available for every product — a typo raises. |
| `epoch` / `epochs` | A single year or an explicit list of reference years (overrides the date window). |
| `start` / `end` | When `epoch(s)` are omitted, the 5-yearly GHSL epochs in this window are fetched. A window narrower than the 5-year step snaps to the nearest epoch (logged). |
| `resolution` | Source variant to download. Metric resolutions (`10m`/`100m`/`1km`) are Mollweide (ESRI:54009); arc-second (`3ss`/`30ss`) are WGS84. Defaults to each product's `default_resolution`. |
| `crs` | **Output** CRS (default `EPSG:4326`). The source is reprojected to it; when it already matches the source CRS, reprojection is skipped. |
| `tiling` | `"auto"` selects + mosaics the intersecting Mollweide tiles for fine resolutions; `"global"` always downloads the whole-globe file. |
| `api` | `"direct"` (deterministic URL builder, the default). `"stac"` is reserved — no queryable JRC STAC API exists yet, so it raises a clear error. |

## Output

`download()` returns the written paths:

* **Raster products** — one GeoTIFF per `(product, epoch)`, named
  `{product}_E{epoch}_{resolution}_epsg{code}.tif`, reprojected to `crs=` and
  cropped to the AOI.
* **Categorical products** (SMOD, BUILT-C, WUP-DEGURBA) — additionally carry a
  `{file}.legend.json` sidecar (class code → label) and use nearest-neighbour
  resampling.
* **Tabular products** (DUC, WUP statistics) — the table is extracted into a
  `{product}/` directory under `path` (no reprojection / crop).

## Multi-epoch time series + `aggregate=`

A window spanning several GHSL epochs writes one GeoTIFF per epoch:

```python
EarthLens(
    data_source="ghsl", variables=["GHS_POP"],
    start="2000-01-01", end="2020-12-31",        # -> 2000, 2005, 2010, 2015, 2020
    lat_lim=[40.3, 40.5], lon_lim=[-3.8, -3.6], path="out/",
).download()
```

Pass `aggregate=` to reduce the per-epoch stack (continuous products only):

```python
from earthlens.aggregate import AggregationConfig

EarthLens(
    data_source="ghsl", variables=["GHS_BUILT_S"],
    start="1975-01-01", end="2020-12-31",
    lat_lim=[40.3, 40.5], lon_lim=[-3.8, -3.6], path="out/",
).download(
    aggregate=AggregationConfig(freq="100YS", op="max"),  # one across-epoch raster
)
```

`freq` buckets the discrete epochs into windows (a coarse `freq` collapses all
epochs into one output); `op` is `mean` / `sum` / `min` / `max` / `std`
(`auto` resolves to `mean`). Aggregating a **categorical** product is rejected
— averaging class codes is meaningless.

## Notes & gotchas

* **Ocean AOIs** — a fine-resolution request whose AOI intersects no land tile
  raises a clear error (the Mollweide land grid is sparse). Use a land AOI, a
  coarse whole-globe resolution, or `tiling="global"`.
* **Whole-globe size** — coarse (1 km / 30″) products are single ~300 MB files;
  the first download is slow, then cached.
* **GHS-LAND** is only published at release `R2022A`; with `release=None`
  (the default) it auto-resolves there, so no explicit release is needed.
* See [Available datasets](products.md) for the full availability matrix.
