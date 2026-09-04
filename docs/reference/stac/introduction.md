# STAC backend — introduction

<img src="../../_images/logos/stac.png" alt="STAC (SpatioTemporal Asset Catalog) logo" height="60">

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
| `eodc` | Earth Observation Data Centre | Copernicus GFM + EODC raster collections (SAR σ0/γ0, soil moisture, DEM, land cover, orthophotos) | **anonymous** |

The model extends to USGS Landsat (`aws-requester-pays`) and other public STAC
catalogues (DE Africa, DEA, Brazil Data Cube, VEDA, EODC) by adding a catalog row.

### Copernicus GFM (flood monitoring)

The `eodc` endpoint serves **Copernicus Global Flood Monitoring** — Sentinel-1
SAR near-real-time flood mapping — as the collection `eodc/gfm`, anonymously. It
exposes twelve single-band `uint8` COG layers (nodata `255`): the final ensemble
products `ensemble_flood_extent` (the default), `ensemble_water_extent`,
`ensemble_likelihood`, plus `reference_water_mask`, `exclusion_mask`,
`advisory_flags`, and the per-algorithm `dlr_` / `tuw_` / `list_` flood-extent
and likelihood intermediates. It is the live-observed flood-extent complement to
the modelled `aqueduct`, the return-period `jrc`, and the impact
`hanze` / `flodis` backends.

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="eodc",  # or data_source="stac", endpoint="eodc"
    start="2022-08-25", end="2022-09-30",
    variables={"eodc/gfm": ["ensemble_flood_extent"]},
    lat_lim=[26.0, 28.0], lon_lim=[67.0, 69.0],
    path="gfm-out",
)
paths = el.download()  # one COG per (collection, acquisition date)
```

GFM is free under the Copernicus licence (attribution; see
[extwiki.eodc.eu/en/GFM](https://extwiki.eodc.eu/en/GFM)).

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

earthlens adds the **CDSE S3 provider signer** (`CdseS3Signer`; Planetary
Computer signing uses pyramids' native `PlanetaryComputerSigner`), the
**endpoint × collection × asset catalog**, and the **search → load → write**
orchestration. There is no `odc-stac` / `stackstac` dependency.

## The catalog is generated, not hand-written

The per-endpoint catalog files (`src/earthlens/stac/catalog/*.yaml`, ~5k lines)
are **machine-generated** from each provider's live STAC `item_assets` by
`tools/stac/refresh_stac_catalog.py` (collection index + band stanzas) and
`tools/stac/probe_stac_assets.py` (per-item `dtype` / `nodata` fill). This is
**by design** — the catalog tracks hundreds of upstream collections, so it is
regenerated rather than edited by hand. Two consequences follow from the source
data, not from earthlens:

* Collections whose `item_assets` publish no `eo:bands` / `raster:bands` (mostly
  CDSE `clms_*_cog`) carry bare asset keys with no `dtype` / `nodata`; for those
  `_nodata_for` falls back to `0`. Re-run the probe tool to fill any that later
  start publishing band metadata.
* Bulk entries omit `extent` / `cadence` / `resolution` — these are
  informational and not required by the download path.

Prefer regenerating via the refresh/probe tools over hand-editing the YAML.

### `asset_aliases` is the one hand-curated block

`asset_aliases` is the exception to the rule above: it is written by hand and
**must survive regeneration**. It exists because an endpoint may publish the
same band under a different key than the catalog names it by — CDSE splits
Sentinel-2 per resolution, so the catalog's `B04` is `B04_10m` there:

```yaml
sentinel-2-l2a:
  aliases:
    cdse: sentinel-2-l2a          # per-endpoint collection id
  asset_aliases:
    cdse:                          # per-endpoint asset keys
      B02: B02_10m
      B04: B04_10m
```

`aliases` overrides the *collection id*; `asset_aliases` overrides the *asset
keys*, one level down. Only the endpoints that rename need an entry, and an
asset an endpoint does not rename passes through unchanged.

The rename is applied at exactly one place — the STAC item lookup. The request's
asset list keeps the catalog's own naming everywhere else, so the `nodata`
lookup and the written band names still match the catalog. Both keys are
validated at load: an endpoint that no `endpoints:` block declares, or an asset
the collection does not carry, is rejected rather than silently ignored.

Resolve them with `Catalog.resolve_assets(endpoint, collection_key, assets)`,
which returns the keys to request from that endpoint, in the given order.

## Output kind & aggregation

`OUTPUT_KIND = "raster"`, so the `EarthLens` facade **forwards**
`aggregate=AggregationConfig(...)` to the backend (it is not rejected as it is
for vector/tabular backends). A multi-date pull is then reduced per time window
into per-window COGs — see [Usage](usage.md).

## See also

* [Usage](usage.md) — request shape, endpoints, aggregation, antimeridian.
* [Authentication](authentication.md) — anonymous / MPC / CDSE signing.
* [API reference](stac.md) — the rendered `earthlens.stac` API.
