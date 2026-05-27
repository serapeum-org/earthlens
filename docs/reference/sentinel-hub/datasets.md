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
| `sentinel-3-olci` | `SENTINEL3_OLCI` | 300 m | 2-day | B08, B06, B04 |
| `sentinel-5p-l2` | `SENTINEL5P` | 3500 m | daily | NO2 |

Each collection's `bands` map carries per-band metadata (common name, units,
native resolution, central wavelength). Sentinel-2 L1C/L2A expose the full MSI
band set (`B01`–`B12`, `B8A`, plus `SCL` on L2A); Sentinel-5P exposes the
atmospheric columns `CO`, `NO2`, `O3`, `SO2`, `CH4`.

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

### Statistical recipes (`kind="stats"`)

Each adds a `dataMask` output band so the Statistical API can exclude invalid
pixels from the zonal stats.

| Key | Evalscript | Bands | Description |
|---|---|---|---|
| `sentinel-2-l2a-ndvi-stats` | `ndvi_stats.js` | B04, B08 | NDVI for the Statistical API |
| `sentinel-2-l2a-ndwi-stats` | `ndwi_stats.js` | B03, B08 | NDWI for the Statistical API |
| `sentinel-2-l2a-evi-stats` | `evi_stats.js` | B02, B04, B08 | EVI for the Statistical API |
| `sentinel-2-l2a-bsi-stats` | `bsi_stats.js` | B02, B04, B08, B11 | BSI for the Statistical API |

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
# or from the live Catalog API (needs SH_CLIENT_ID / SH_CLIENT_SECRET)
python tools/sentinel_hub/refresh_sh_catalog.py refresh
python tools/sentinel_hub/refresh_sh_catalog.py refresh --from-sdk --dry-run

# validate a recipe (its .js exists, is //VERSION=3, stats recipes carry dataMask)
python tools/sentinel_hub/refresh_sh_catalog.py validate-recipe sentinel-2-l2a-ndvi
python tools/sentinel_hub/refresh_sh_catalog.py validate-all
```

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
