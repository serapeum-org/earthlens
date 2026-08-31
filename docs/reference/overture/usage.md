# Overture Maps — Usage

This page covers the request shape, every keyword argument, the output,
the bbox-size guard, and why temporal / `aggregate=` arguments do not
apply. For authentication: there is none — the bucket is public and
anonymous, so `pip install earthlens[overture]` is the only setup.

## Request shape

```python
from earthlens.core import EarthLens

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
| `release` | `None` | Overture release id (`"2026-07-22.0"`). `None` targets the release Overture publishes now — auto-targeted by the SDK on the default path, resolved live on the DuckDB path. List them with the refresh tool or `Catalog().available_releases`. Pinning is reproducible only until Overture prunes that release from S3. |
| `file_format` | `"geoparquet"` | Output format: `"geoparquet"` (default, lossless nested schema), `"gpkg"`, or `"geojson"`. |
| `max_features` | `None` | Cap on rows kept per fetched type. When set, the read **streams** (via `record_batch_reader`) and stops early once the cap is reached, rather than fetching the whole bbox and discarding rows. `None` keeps all. |
| `stream` | `False` | Force the streaming `record_batch_reader` path (lower peak memory) even without `max_features`. Streaming is used automatically whenever `max_features` is set. |
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
out/overture_buildings_building_2026-07-22.0.parquet
out/overture_places_place_latest.parquet
```

Which of the two you get follows one rule: **a name carries a release id when
the backend knows it**, and `latest` when it does not.

| Fetch | Filename stamp |
|-------|----------------|
| Any fetch with `release=` pinned | the pinned id |
| Unpinned `where=` / `columns=` (DuckDB) | the release resolved for the glob |
| Unpinned plain fetch (SDK) | `latest` |

The SDK path is the odd one out because it hands `release=None` to the SDK and
is never told which snapshot answered. Two consequences worth knowing:

- A `_latest` file is **overwritten** by the next run, including across a
  monthly rollover — convenient for a scratch directory, lossy if you meant to
  keep both. A release-stamped file is not, so an unpinned DuckDB fetch
  accumulates one file per release rather than overwriting.
- The stamp does not encode `where=` / `columns=`, so a filtered and an
  unfiltered fetch of the same theme, type, and release write to the **same
  path** and the second silently replaces the first. Give them separate
  `path=` directories when you need both.

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

The `earthlens datasets` commands maintain the bundled catalog:

```bash
# Diff the live SDK / STAC release list against the bundled index
earthlens datasets refresh overture

# ... and rewrite the available_releases index from it
earthlens datasets refresh overture --write

# Confirm every curated theme/default-type resolves against live data
earthlens datasets validate overture --live

# Inspect one type's columns when curating a new theme
earthlens datasets probe overture building
```

`No files found that match the pattern` from a DuckDB fetch means the release
being globbed holds no objects on S3. It has more than one cause, and the log
line just above it tells you which:

- **A pinned release that has been pruned.** Drop the pin, or move it to one
  Overture still publishes — the refresh above lists them. Most common.
- **The live lookup failed and the backend fell back to the bundled index.**
  A `WARNING` naming the bundled id precedes the error. This is a
  reachability problem with `https://stac.overturemaps.org`, not an index
  problem, so refreshing will not fix it.
- **A theme/type that exists but is empty for that release** — rare, and the
  bbox guard usually catches the mistake first.

Refreshing the index is the right first move only for the first cause.

## Streaming vs in-memory reads

By default each type is materialised in one shot with the SDK's
`geodataframe`. For a large bounded bbox, pass `stream=True` to read through
the SDK's `record_batch_reader` instead — features are assembled batch by
batch, lowering peak memory. When `max_features` is set the backend streams
**and stops early** once the cap is reached, so it never downloads the whole
bbox just to discard the surplus:

```python
EarthLens(
    data_source="overture",
    variables={"places": []},
    lat_lim=[40.74, 40.76],
    lon_lim=[-74.0, -73.97],
    path="out",
    stream=True,          # batch-by-batch read
    max_features=5000,    # stop after ~5000 rows
).download()
```

## Attribute pushdown with `where=` (DuckDB)

The bbox limits *where* you fetch; `where=` limits *which rows* — pushed down
to the GeoParquet on S3 via DuckDB, so only matching features leave the
bucket (not the whole bbox). Pass a raw SQL predicate; it is ANDed onto the
bbox filter. `columns=` optionally narrows the projection (`id` and `sources`
are always kept so identity and the per-row `license_id` survive).

```python
EarthLens(
    data_source="overture",
    variables={"buildings": []},
    lat_lim=[40.74, 40.76],
    lon_lim=[-74.0, -73.97],
    path="out",
    where="height > 10 AND subtype = 'residential'",   # pushed down to S3
    columns=["names", "height", "subtype"],            # narrow the projection
    max_features=10000,                                 # LIMIT
).download()
```

Notes:

- The predicate is **raw SQL** evaluated by DuckDB against the parquet
  schema — you can filter on nested fields (`categories.primary = 'restaurant'`,
  `names.primary IS NOT NULL`), numeric attributes (`confidence > 0.9`), etc.
- The public bucket is read **anonymously** (no AWS credentials needed, and any
  in your environment are bypassed).
- Requires `duckdb` — included in `pip install earthlens[overture]`.
- Setting `where=` takes precedence over `stream=`; `max_features` becomes a
  SQL `LIMIT`.

## Known limitations

- **No DuckDB attribute pushdown without `where=`** — the plain fetch path uses
  the SDK's PyArrow bbox pushdown only; attribute filtering needs `where=`.
- **No temporal axis** — pin a `release` for reproducibility; `None` follows
  the newest monthly release. A pin is reproducible only while that release
  exists: Overture keeps the newest one (or two) on S3 and prunes the rest,
  after which the pinned id resolves to nothing.
- **`base` types carry mixed geometries** — `land` / `water` /
  `infrastructure` return a mix of polygons, lines, and points. Filter
  `geometry.geom_type` client-side if you need a single kind.
