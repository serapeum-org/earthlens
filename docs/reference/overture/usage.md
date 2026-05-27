# Overture Maps — Usage

This page covers the request shape, every keyword argument, the output,
the bbox-size guard, and why temporal / `aggregate=` arguments do not
apply. For authentication: there is none — the bucket is public and
anonymous, so `pip install earthlens[overture]` is the only setup.

## Request shape

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="overture",
    variables={"buildings": ["building"]},
    lat_lim=[40.757, 40.759],
    lon_lim=[-73.987, -73.984],
    path="out",
    release=None,            # default: newest release
    file_format="geoparquet",
    max_features=None,
).download()
```

### `variables` — `{theme: [type, ...]}`

`variables` is a **mapping** of friendly theme name to a list of feature
types (it is *not* a list of variable names as for the raster backends —
passing a list raises `TypeError`):

- `{"buildings": []}` — the theme's **primary type** (`building`).
- `{"places": ["place"]}` — an explicit type.
- `{"transportation": ["segment", "connector"]}` — several types; one
  file is written per type.
- `{"buildings": [], "places": []}` — several themes at once.

An empty type list resolves to the theme's `default_type`. An unknown
theme or type raises `ValueError` with a did-you-mean hint.

### `lat_lim` / `lon_lim` — the bbox (required)

The bounding box drives the GeoParquet **bbox pushdown** (via PyArrow
parquet statistics — no DuckDB), so only the rows inside the box are read.
`lat_lim=[south, north]`, `lon_lim=[west, east]`, in degrees (WGS84). A
**bounded** bbox is required — see the size guard below.

## Keyword arguments

| Kwarg | Default | Meaning |
|-------|---------|---------|
| `release` | `None` | Overture release id (`"2026-05-20.0"`). `None` lets the SDK auto-target the newest release. List them with the refresh tool or `Catalog().available_releases`. |
| `file_format` | `"geoparquet"` | Output format: `"geoparquet"` (default, lossless nested schema), `"gpkg"`, or `"geojson"`. |
| `max_features` | `None` | Cap on rows kept per fetched type; excess rows are dropped (head) with a warning. `None` keeps all. |
| `max_bbox_deg2` | `None` | Override the per-theme bbox-area cap (square degrees) for the guarded themes. `None` uses the built-in caps. |
| `start` / `end` | `None` | Accepted for signature parity but **ignored** — Overture is a static per-release snapshot. |
| `temporal_resolution` | `"all"` | Sentinel; Overture is not chunked in time. |

## The bbox-size guard

Buildings (2.3 B rows) and Transportation (86 M km) cover the whole planet
at row counts where an unbounded box is a footgun. The backend therefore
**rejects an oversized or whole-Earth bbox** for the large themes:

| Theme | Default cap |
|-------|-------------|
| `buildings` | 0.5 deg² |
| `transportation` | 0.5 deg² |
| `base` | 0.5 deg² |
| `addresses` | 0.5 deg² |
| `places` | 9.0 deg² |
| `divisions` | unguarded (few rows globally) |

One square degree is ≈ 12 300 km² at the equator. Exceeding a theme's cap
raises `ValueError` with guidance to shrink the bbox, raise
`max_bbox_deg2=`, or cap with `max_features=`. A bounded bbox keeps the
PyArrow pushdown cheap.

```python
# This raises: a whole-Earth buildings request is rejected.
EarthLens(
    data_source="overture",
    variables={"buildings": []},
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    path="out",
).download()
```

## Output / return value

`download()` returns a `list[Path]` — one written file per requested
feature type. Filenames embed the theme, type, and release:

```
out/overture_buildings_building_2026-05-20.0.parquet
out/overture_places_place_latest.parquet
```

Every output carries a per-row **`license_id`** column and is tagged
`EPSG:4326`. The SDK returns a CRS-less frame, so the backend sets the CRS
explicitly before writing.

## Output kind & `aggregate=`

Overture declares `OUTPUT_KIND = "vector"`. The `EarthLens` facade rejects
a non-`None` `aggregate=` for vector backends with `NotImplementedError`
(the aggregator only reduces gridded rasters). Post-process the returned
`GeoDataFrame` directly instead.

## Rate limits / quotas

None imposed by Overture — the bucket is public and anonymous. The
practical limit is the bbox size (the pushdown reads only the box), which
the size guard protects you from misusing.

## Catalog tooling

`tools/overture/refresh_overture_catalog.py` maintains the bundled
catalog:

```bash
# Rewrite the available_releases index from the live SDK / STAC catalog
pixi run -e dev python tools/overture/refresh_overture_catalog.py refresh

# Confirm every curated theme/default-type resolves against live data
pixi run -e dev python tools/overture/refresh_overture_catalog.py validate --strict

# Inspect one type's columns when curating a new theme
pixi run -e dev python tools/overture/refresh_overture_catalog.py probe building
```

## Known limitations

- **No DuckDB / SQL pushdown** — only the SDK's PyArrow bbox pushdown.
- **No temporal axis** — pin a `release` for reproducibility; `None` drifts
  to the newest monthly release.
- **`base` types carry mixed geometries** — `land` / `water` /
  `infrastructure` return a mix of polygons, lines, and points. Filter
  `geometry.geom_type` client-side if you need a single kind.
