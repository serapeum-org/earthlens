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

To fetch **any** of the ~41k HDX datasets without a catalogue row, pass
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

By default `download()` returns native-format file paths, leaving reading
to the caller:

```python
import geopandas as gpd

paths = EarthLens(
    data_source="hdx", variables={"cod-ab-kenya": ["GeoJSON"]}, path="data/hdx"
).download()
gdf = gpd.read_file(paths[0])
```

### `read=True` — read into pyramids types directly

Pass `read=True` to have each downloaded resource read into its pyramids
type — a `Dataset` (raster), a `FeatureCollection` (vector), or a
`DataFrame` (tabular) — dispatched by the recorded CKAN format label,
with `.gz` / `.zip` containers transparently decompressed:

```python
objs = EarthLens(
    data_source="hdx", variables={"cod-ab-kenya": ["GeoJSON"]}, path="data/hdx"
).download(read=True)
# objs[0] is a pyramids FeatureCollection (a GeoJSON resource)
```

`read=True` uses `pyramids.read_resource`, available in **`pyramids-gis
>= 0.29.0`** — which is the package floor, so it works out of the box.
(On a partial / source pyramids install that predates the reader,
`read=True` raises `NotImplementedError` with an upgrade hint; the reader
is feature-detected at call time, and `read=False` always works.)

## Rate limits & gotchas

- HDX is public and unauthenticated; be considerate with large batches.
- Some resources (Kontur global GeoPackage, HRSL country bundles) are
  **large** — filter to one resource and a small country where possible.
- `Configuration.create` is a process-global singleton; constructing
  several `HDX` instances reuses the one configuration (the backend
  guards against the SDK's re-create error).
