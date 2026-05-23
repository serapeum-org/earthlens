# STAC backend — introduction

`earthlens.stac` is **one unified backend** over the STAC-API + Cloud-Optimized
GeoTIFF (COG) providers. They all speak **STAC API v1**; the only per-provider
difference is **how an asset is signed for reading**, so a single backend covers
them all and selects the right signer from the catalog.

## Endpoints

| Endpoint key | Provider | Best for | Signing |
|---|---|---|---|
| `planetary-computer` | Microsoft Planetary Computer | broad catalogue (~120 collections) | Azure **SAS** URL signing (no account) |
| `cdse` | Copernicus Data Space Ecosystem | every Sentinel mission | **S3** credentials (eodata store) |
| `earth-search` | Element 84 / AWS Open Data | anonymous Sentinel-2 COG, Landsat C2, Copernicus DEM | **anonymous** |

The model extends to USGS Landsat (`aws-requester-pays`) and other public STAC
catalogues (DE Africa, DEA, Brazil Data Cube, VEDA) by adding a catalog row.

## What it returns

Gridded **`raster`** output: one COG per `(collection, acquisition date)` is
written to your `path`, and `download()` returns the list of written COG paths.
A request that crosses the antimeridian yields one COG per side
(`…_part0` / `…_part1`).

## How it is built

The GIS work lives in **pyramids** (the shared GIS backend); earthlens owns only
the provider glue:

* `pyramids.stac` — the STAC client + the generic `Signer` protocol
  (`AnonymousSigner`, `AWSRequesterPaysSigner`, `BearerTokenSigner`) and
  `load_asset`.
* `pyramids.dataset` — `merge_rasters` / `stack_bands` (tile mosaic + band
  stack), `DatasetCollection` (the time-window reducer behind `aggregate=`),
  and `cog.write_cog`.
* `pyramids.feature.bbox.split_antimeridian` — antimeridian handling.

earthlens adds the **provider signers** (`MpcSasSigner`, `CdseS3Signer`), the
**endpoint × collection × asset catalog**, and the **search → load → write**
orchestration. There is no `odc-stac` / `stackstac` dependency.

## Output kind & aggregation

`OUTPUT_KIND = "raster"`, so the `EarthLens` facade **forwards**
`aggregate=AggregationConfig(...)` to the backend (it is not rejected as it is
for vector/tabular backends). A multi-date pull is then reduced per time window
into per-window COGs — see [Usage](usage.md).

## See also

* [Usage](usage.md) — request shape, endpoints, aggregation, antimeridian.
* [Authentication](authentication.md) — anonymous / MPC / CDSE signing.
* [API reference](stac.md) — the rendered `earthlens.stac` API.
