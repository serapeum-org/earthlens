# Humanitarian Data Exchange (HDX) — usage

## Request shape

HDX has no bbox/time query, so the request is a mapping from a curated
**dataset key** to a list of **resource filters**:

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="hdx",
    variables={"hotosm-uganda-buildings": ["Geopackage"]},
    path="data/hdx",
).download()
# -> [PosixPath('data/hdx/hotosm_uga_buildings_gpkg.zip'), ...]
```

`download()` returns a `list[pathlib.Path]` — the local paths of the
downloaded resource files, in dataset/resource order.

### `variables` — dataset key → resource filters

- The **key** is a curated catalogue key (see
  [Catalog & tooling](catalog.md) for the full list).
- The **value** is a list of resource filters. Each filter is either:
    - a **resource-name glob**, e.g. `"*.gpkg.gz"`, `"*.csv"`; or
    - a **CKAN format label**, e.g. `"Geopackage"`, `"CSV"`,
      `"GeoTIFF"`, `"SHP"` (case-insensitive).
- An **empty list** falls back to the catalogue row's default
  `resource_filter`; if that is also empty, **every** resource of the
  dataset is downloaded.

```python
EarthLens(data_source="hdx", variables={"kontur-population": []})          # default filter
EarthLens(data_source="hdx", variables={"cod-ab-kenya": ["GeoJSON"]})      # by format label
EarthLens(data_source="hdx", variables={"hotosm-srilanka-roads": ["*.shp.zip"]})  # by name glob
```

Multiple datasets in one call are allowed:

```python
EarthLens(
    data_source="hdx",
    variables={"worldpop-kenya": [], "worldpop-ethiopia": []},
)
```

### The `hdx_id=` / `resource=` escape hatch

To fetch **any** of the ~21k HDX datasets without a catalogue row, pass
`hdx_id=` (and optionally `resource=`). It bypasses the catalogue:

```python
EarthLens(
    data_source="hdx",
    variables={},
    hdx_id="wfp-food-prices",
    resource="*.csv",
    path="data/hdx",
).download()
```

`resource=` accepts a single filter string or a list of them.

### Other keyword arguments

| Kwarg | Default | Meaning |
|-------|---------|---------|
| `hdx_site` | `"prod"` | HDX site to target (`"prod"` / `"stage"`). |
| `user_agent` | `"earthlens"` | User agent string the SDK requires. |
| `hdx_id` | `None` | Arbitrary HDX dataset id (escape hatch). |
| `resource` | `None` | Resource filter(s) for the escape hatch. |
| `progress_bar` | `True` | Best-effort progress signal on download. |

### Ignored arguments (`bbox` / time)

`lat_lim` / `lon_lim` / `start` / `end` are accepted (the facade
requires a bbox) but **not used for the query** — CKAN cannot be
queried by space or time. They default to the whole globe and a wide
date window and never narrow the resource selection.

## Output kind & `aggregate=`

`HDX.OUTPUT_KIND` is the fixed value `"mixed"`. The aggregator does not
apply to arbitrary resource files, so passing `aggregate=` raises:

```python
EarthLens(data_source="hdx", variables={"kontur-population": []}).download(
    aggregate=some_config
)
# NotImplementedError: HDX returns resource files as-is ... aggregate= is not applicable.
```

## Reading the downloaded files

The MVP returns files in their native formats. Reading them is up to
the caller (e.g. `pyramids` for GeoPackage / GeoTIFF, `pandas` for CSV):

```python
import geopandas as gpd

paths = EarthLens(
    data_source="hdx", variables={"cod-ab-kenya": ["GeoJSON"]}, path="data/hdx"
).download()
gdf = gpd.read_file(paths[0])
```

## Rate limits & gotchas

- HDX is public and unauthenticated; be considerate with large batches.
- Some resources (Kontur global GeoPackage, HRSL country bundles) are
  **large** — filter to one resource and a small country where possible.
- `Configuration.create` is a process-global singleton; constructing
  several `HDX` instances reuses the one configuration (the backend
  guards against the SDK's re-create error).
