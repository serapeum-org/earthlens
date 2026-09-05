# Area of interest (`earthlens.base.spatial`)

Every request needs to know *where*. There are two **mutually exclusive** channels:

- **`aoi=`** — one argument that accepts seven different shapes;
- **`lat_lim=` / `lon_lim=`** — the legacy pair, each `[min, max]` in degrees.

Passing both raises `ValueError: pass either aoi= or lat_lim=/lon_lim=, not both`. Passing neither requests the
whole globe (`lat_lim=[-90, 90]`, `lon_lim=[-180, 180]`).

Whatever goes into `aoi=` is normalised by `resolve_aoi` down to that same `lat_lim` / `lon_lim` pair — so no
backend has to change — and a genuinely non-rectangular shape is kept alongside it as a **clip mask**.

Every form below is demonstrated end to end, against anonymous GEBCO, in the
[Choosing an area of interest](../../examples/basics/01_area_of_interest.ipynb) notebook.

## The accepted `aoi` forms

| Form | Example | Mask kept? |
|------|---------|:----------:|
| bbox sequence | `aoi=[-29.5, 36.2, -27.7, 38.0]` | — |
| bbox mapping | `aoi={"min_lon": -29.5, "min_lat": 36.2, "max_lon": -27.7, "max_lat": 38.0}` | — |
| point + `buffer=` | `aoi=(-28.6, 37.1), buffer=0.9` | — |
| WKT string | `aoi="POLYGON((-29.5 36.2, ...))"` | ✅ |
| GeoJSON mapping | `aoi={"type": "Polygon", "coordinates": [...]}` | ✅ |
| `GeoDataFrame` / `GeoSeries` | `aoi=gdf` | ✅ (polygonal frames) |
| shapely geometry | `aoi=Polygon([...])` | ✅ |

All coordinates are read as **WGS84 degrees**, with one exception — a `GeoDataFrame` that declares another CRS,
which is reprojected for you (see below).

### Coordinate order

The bbox sequence is **`[W, S, E, N]`** — the GeoJSON / STAC order, longitude first. That is deliberately *not*
the order of the legacy pair, which is latitude-first (`lat_lim=[S, N]`, `lon_lim=[W, E]`). The same box written
both ways:

```python
lens = EarthLens(data_source="gebco", aoi=[-29.5, 36.2, -27.7, 38.0])
lens = EarthLens(data_source="gebco", lat_lim=[36.2, 38.0], lon_lim=[-29.5, -27.7])
```

### Bbox mappings accept several key spellings

A mapping that is *not* GeoJSON is read as the four bbox edges. Each edge is resolved against a list of aliases —
GeoJSON `min_lon`, eodag `lonmin`, shapely / geopandas `minx`, compass `west` — matched **case-insensitively**, so
a box arriving as another tool's JSON usually needs no translation. A missing edge is named in the error
(`no key found for the 'north' edge`).

### Points need a `buffer`

A two-value `aoi` is read as `(lon, lat)`. A point has no area, so `buffer=` — a half-width in degrees — is
**required** rather than defaulted, and the resulting square is clamped to the valid lon/lat ranges. `buffer=`
without a point `aoi=` raises.

### GeoDataFrames are reprojected for you

The check is duck-typed on `total_bounds`, so `geopandas` is never imported merely to test a type, and a
`GeoSeries` works as well as a `GeoDataFrame`. Because `total_bounds` reports in the frame's *own* CRS, a
projected frame is reprojected to EPSG:4326 first — otherwise a UTM or Web Mercator frame would yield a
metre-valued, out-of-range bbox. A frame with no declared CRS is taken as already lon/lat.

Only **polygonal** frames contribute a mask; a points or lines frame contributes its bounding box alone.

## Polygon masks are honoured only where supported

`resolve_aoi` always returns the mask when the input had a real shape, but whether it is *used* is the backend's
decision, advertised through `SUPPORTS_POLYGON_AOI` (see [Base contracts](contracts.md)). A backend that cannot
clip to a polygon falls back to the bounding box and emits a
[`PolygonAoiWarning`](extents.md#polygonaoiwarning). Its own docstring says why it exists: the download still
succeeds, it just covers the bbox, and that is "the most dangerous kind of wrong result — a valid raster of
the right variable over roughly the right area". Watch for that warning whenever you pass a polygon.

Backends that do support it apply the mask through `crop_to_aoi` (crop and mask in one step) or
`mask_to_geometry` (mask a raster that some other route already cropped to the bbox, such as a server-side
`area` parameter). Masking a raster that declares no no-data value logs a warning, because the mask can then only
trim to the polygon's bounding box.

## Inputs that are rejected rather than guessed

| Input | Outcome |
|-------|---------|
| a point `aoi` with no `buffer=` | `ValueError` — a point has no area |
| west east of east, e.g. `[170, -20, -170, -10]` | `ValueError` — an antimeridian crossing (see below) |
| south north of north | `ValueError` — inverted latitude bounds |
| any other type | `TypeError` naming the accepted forms |

A west-of-east box is the GeoJSON / STAC spelling of an **antimeridian crossing**, not a typo, so it is named
rather than silently reinterpreted. Split it at ±180 and issue the two halves as separate requests — the error
message spells out both boxes for you.

## Backends that interpret `aoi=` themselves

One backend declares its own, richer `aoi` parameter and therefore receives the value **verbatim**, bypassing
`resolve_aoi`: **`worldpop`**, whose `aoi=` also accepts an ISO3 country code or a list of them. Passing
`buffer=` to such a backend raises, since the buffer is a `resolve_aoi` concept.

## API

::: earthlens.base.spatial.resolve_aoi

::: earthlens.base.spatial.normalize_aoi

::: earthlens.base.spatial.crop_to_aoi

::: earthlens.base.spatial.mask_to_geometry
