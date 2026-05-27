# Sentinel Hub — collections & evalscript recipes

The catalog has **two layers** (the headline design decision): curated **data
collections** and a bundled library of **evalscript recipes**. A request names
either a recipe (which pins its collection + render logic) or a collection (plus
an explicit `evalscript=`).

The catalog ships as package data under
`src/earthlens/sentinel_hub/catalog/` and the evalscript `.js` files under
`src/earthlens/sentinel_hub/evalscripts/`. Load it directly with:

```python
from earthlens.sentinel_hub import Catalog
cat = Catalog()
cat.is_recipe("sentinel-2-l2a-ndvi")            # True
cat.get_collection("sentinel-2-l2a").sh_collection   # 'SENTINEL2_L2A'
cat.resolve("sentinel-2-l2a-ndvi").evalscript        # 'ndvi.js'
```

## Curated collections

| Key | `DataCollection` | Resolution | Cadence | Default bands |
|---|---|---|---|---|
| `sentinel-2-l1c` | `SENTINEL2_L1C` | 10 m | 5-day | B04, B03, B02 |
| `sentinel-2-l2a` | `SENTINEL2_L2A` | 10 m | 5-day | B04, B03, B02 |
| `sentinel-1-iw` | `SENTINEL1_IW` | 10 m | 6-day | VV, VH |
| `sentinel-1-ew` | `SENTINEL1_EW` | 40 m | 6-day | HH, HV |
| `sentinel-3-olci` | `SENTINEL3_OLCI` | 300 m | 2-day | B08, B06, B04 |
| `sentinel-3-slstr` | `SENTINEL3_SLSTR` | 500 m | daily | S3, S2, S1 |
| `sentinel-5p-l2` | `SENTINEL5P` | 3500 m | daily | NO2 |
| `landsat-ot-l1` | `LANDSAT_OT_L1` | 30 m | 16-day | B04, B03, B02 |
| `dem-copernicus-30` | `DEM_COPERNICUS_30` | 30 m | static | DEM |

Each collection's `bands` map carries per-band metadata (common name, units,
native resolution, central wavelength). Sentinel-2 L1C/L2A expose the full MSI
band set (`B01`–`B12`, `B8A`, plus `SCL` on L2A); Sentinel-5P exposes the
atmospheric columns (`CO`, `NO2`, `O3`, `SO2`, `CH4`); Sentinel-3 SLSTR adds the
thermal brightness-temperature bands (`S7`–`S9`, `F1`/`F2`); Landsat OT L1 is
top-of-atmosphere optical + thermal; `DEM_COPERNICUS_30` is a single elevation
band.

!!! note "Default endpoint = CDSE-free"
    The curated collections above are all served by the **default CDSE-free
    endpoint**. The `sentinelhub` SDK also knows other collections — **MODIS**,
    **Landsat L2 surface reflectance** (`LANDSAT_OT_L2`), and **HLS**
    (`HARMONIZED_LANDSAT_SENTINEL`) — that CDSE does **not** serve (they return a
    `RENDERER_EXCEPTION` "Unable to resolve"). Those are available on the
    **commercial** Sentinel Hub deployment: pass `endpoint="commercial"` with a
    commercial subscription and address them via a custom `evalscript=`.

The full set of collection ids Sentinel Hub can serve is kept in the
informational `available_collections` index (rebuilt by the
[refresh tool](#catalog-tooling)); the table above is the **curated** subset with
vetted band metadata.

## Curated evalscript recipes

Recipes are split into **render** recipes (write a raster) and **stats** recipes
(emit the `dataMask` band the Statistical API requires).

### Render recipes (`kind="render"`)

| Key | Evalscript | Bands | Output bands | Description |
|---|---|---|---|---|
| `sentinel-2-l2a-ndvi` | `ndvi.js` | B04, B08 | 1 | NDVI (FLOAT32) |
| `sentinel-2-l2a-true-colour` | `true_colour.js` | B02, B03, B04 | 3 | True-colour RGB |
| `sentinel-2-l2a-false-colour` | `false_colour.js` | B03, B04, B08 | 3 | False-colour (vegetation) |
| `sentinel-2-l2a-ndwi` | `ndwi.js` | B03, B08 | 1 | NDWI (water) |
| `sentinel-2-l2a-evi` | `evi.js` | B02, B04, B08 | 1 | EVI |
| `sentinel-2-l2a-bsi` | `bsi.js` | B02, B04, B08, B11 | 1 | Bare Soil Index |
| `sentinel-2-l2a-swir-composite` | `swir_composite.js` | B04, B08, B12 | 3 | SWIR composite |
| `sentinel-2-l2a-ndmi` | `ndmi.js` | B08, B11 | 1 | NDMI (moisture) |
| `sentinel-2-l2a-ndsi` | `ndsi.js` | B03, B11 | 1 | NDSI (snow) |
| `sentinel-2-l2a-savi` | `savi.js` | B04, B08 | 1 | SAVI (soil-adjusted veg) |
| `sentinel-2-l2a-nbr` | `nbr.js` | B08, B12 | 1 | NBR (burn ratio) |
| `sentinel-2-l2a-agriculture` | `agriculture.js` | B02, B08, B11 | 3 | Agriculture composite |
| `sentinel-2-l2a-geology` | `geology.js` | B02, B11, B12 | 3 | Geology composite |
| `landsat-ot-l1-ndvi` | `landsat_ndvi.js` | B04, B05 | 1 | Landsat 8/9 L1 NDVI |
| `landsat-ot-l1-true-colour` | `landsat_true_colour.js` | B02, B03, B04 | 3 | Landsat 8/9 L1 true-colour |
| `sentinel-1-iw-rgb-ratio` | `sar_rgb_ratio.js` | VV, VH | 3 | SAR dual-pol composite |
| `dem-copernicus-30-elevation` | `dem_elevation.js` | DEM | 1 | Copernicus DEM elevation |
| `sentinel-3-slstr-thermal` | `slstr_thermal.js` | S8 | 1 | SLSTR thermal BT (Kelvin) |
| `sentinel-3-olci-true-colour` | `olci_true_colour.js` | B04, B06, B08 | 3 | OLCI true-colour |

### Statistical recipes (`kind="stats"`)

Each adds a `dataMask` output band so the Statistical API can exclude invalid
pixels from the zonal stats.

| Key | Evalscript | Bands | Description |
|---|---|---|---|
| `sentinel-2-l2a-ndvi-stats` | `ndvi_stats.js` | B04, B08 | NDVI for the Statistical API |
| `sentinel-2-l2a-ndwi-stats` | `ndwi_stats.js` | B03, B08 | NDWI for the Statistical API |
| `sentinel-2-l2a-evi-stats` | `evi_stats.js` | B02, B04, B08 | EVI for the Statistical API |
| `sentinel-2-l2a-bsi-stats` | `bsi_stats.js` | B02, B04, B08, B11 | BSI for the Statistical API |
| `sentinel-2-l2a-ndmi-stats` | `ndmi_stats.js` | B08, B11 | NDMI for the Statistical API |
| `sentinel-2-l2a-nbr-stats` | `nbr_stats.js` | B08, B12 | NBR for the Statistical API |
| `landsat-ot-l1-ndvi-stats` | `landsat_ndvi_stats.js` | B04, B05 | Landsat 8/9 L1 NDVI for the Statistical API |

## Adding a custom evalscript

You do **not** have to add a recipe to use a custom band-math program. Pass
`evalscript=` (an inline V3 JS string or a path to a `.js` file) together with a
plain collection key:

```python
el = EarthLens(
    data_source="sentinel-hub",
    variables={"sentinel-2-l2a": []},   # plain collection
    evalscript="path/to/my_index.js",   # or an inline V3 string
    ...,
)
```

A custom evalscript for the **Statistical** plane **must** declare a `dataMask`
output band, e.g.:

```javascript
//VERSION=3
function setup() {
  return { input: ["B04", "B08", "dataMask"], output: [
    { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
    { id: "dataMask", bands: 1 } ] };
}
function evaluatePixel(s) {
  return { ndvi: [(s.B08 - s.B04) / (s.B08 + s.B04)], dataMask: [s.dataMask] };
}
```

See the [Evalscript V3 reference](https://docs.sentinel-hub.com/api/latest/evalscript/v3/).

## Catalog tooling

Three CLIs under `tools/sentinel_hub/` maintain and inspect the catalog (the
same trio as the GEE / openEO backends):

**`refresh_sh_catalog.py`** — rebuild the `available_collections` index and
validate recipes:

```bash
# rebuild the index offline from the sentinelhub DataCollection enum (no creds)
python tools/sentinel_hub/refresh_sh_catalog.py refresh --from-sdk
# or from the live Catalog API (needs SENTINELHUB_CLIENT_ID / SENTINELHUB_CLIENT_SECRET)
python tools/sentinel_hub/refresh_sh_catalog.py refresh
python tools/sentinel_hub/refresh_sh_catalog.py refresh --from-sdk --dry-run

# validate a recipe (its .js exists, is //VERSION=3, stats recipes carry dataMask)
python tools/sentinel_hub/refresh_sh_catalog.py validate-recipe sentinel-2-l2a-ndvi
python tools/sentinel_hub/refresh_sh_catalog.py validate-all
```

Prefer `--from-sdk`: it lists the UPPERCASE `DataCollection` names
(`SENTINEL2_L2A`, …) that the backend uses in a collection's `sh_collection`. The
live `refresh` returns STAC-style ids (`sentinel-2-l2a`, …) in a *different*
namespace, so it should only be used when you specifically want the live STAC
ids — otherwise it misaligns the index with the curated catalog.

**`audit_sh_datasets.py`** — flag drift between the curated catalog and the SDK
(curated collections / recipe base collections not in the `DataCollection`
enum, recipe evalscript problems, and untracked enum members); offline:

```bash
python tools/sentinel_hub/audit_sh_datasets.py audit
python tools/sentinel_hub/audit_sh_datasets.py audit --strict   # exit 1 on drift
```

**`probe_sh_collection.py`** — inspect a curated key or a raw `DataCollection`
member when curating a new collection row; `--yaml` emits a paste-ready stanza:

```bash
python tools/sentinel_hub/probe_sh_collection.py sentinel-2-l2a
python tools/sentinel_hub/probe_sh_collection.py SENTINEL3_SLSTR --yaml
```
