# Administrative boundaries — usage

The `admin` backend takes a **dataset id** and the **selector** that dataset
needs, and returns a `pyramids.feature.collection.FeatureCollection` of boundary
polygons in EPSG:4326. Reach it through the `EarthLens` facade with
`data_source="admin"` (aliases: `"admin-boundaries"`, `"geoboundaries"`,
`"natural-earth"`, `"tiger"`).

## Selecting a dataset

```python
from earthlens.core import EarthLens

fc = EarthLens(
    data_source="admin",
    variables=["geoboundaries:adm1"],   # one or more dataset ids
    country="KEN",                       # the selector this dataset needs
).download()
```

- `variables=` takes a list of dataset ids. The common case is one id; several
  ids are concatenated into one combined `FeatureCollection`. See
  [Available datasets](datasets.md) for the ids.
- The selector is **per provider** (see below). A missing required selector
  raises a `ValueError` naming it.
- Spatial arguments (`lat_lim` / `lon_lim` / `aoi`) are accepted for signature
  parity but ignored — admin layers are not bbox-sampled.
- `aggregate=` is rejected: boundaries are vector, not a gridded field.
- Pass `path=` to also write the result to one vector file (`file_format="gpkg"`
  by default, or `"geojson"`); omit it to get the in-memory collection only.

## geoBoundaries (per-country, ADM0–ADM5)

```python
fc = EarthLens(
    data_source="geoboundaries",
    variables=["geoboundaries:adm2"],
    country="KEN",            # ISO3 country code (required)
).download()
```

geoBoundaries is two-step: the backend calls the gbOpen API for the
`(country, ADM level)` pair, reads the `gjDownloadURL` from the metadata, and
reads that GeoJSON. The columns are `shapeName`, `shapeISO`, `shapeID`,
`shapeGroup`, `shapeType`, `geometry`.

## CGAZ (seamless global ADM0/1/2)

```python
fc = EarthLens(
    data_source="admin",
    variables=["cgaz:adm1"],   # one global layer, every country — no selector
).download()
```

CGAZ ships one large seamless GeoPackage per ADM level (hundreds of MB), so a
fetch downloads the whole global layer. Its CRS is an unlabelled geographic-degree
CRS, which the backend declares EPSG:4326 without a transform.

## Natural Earth (countries / states, by scale)

```python
fc = EarthLens(
    data_source="natural-earth",
    variables=["natural_earth:countries"],
    scale="50m",             # optional: 10m / 50m / 110m (defaults per dataset)
).download()
```

Each Natural Earth dataset has a `default_scale` used when `scale=` is omitted
(`110m` for countries, `50m` for states).

## US Census TIGER/Line (states / counties / tracts / nation)

```python
# Nationwide states (NAD83 -> reprojected to EPSG:4326)
fc = EarthLens(
    data_source="tiger",
    variables=["tiger:state"],
    year=2023,               # optional: vintage year (defaults per dataset)
).download()

# Census tracts for one state need a FIPS code
fc = EarthLens(
    data_source="tiger",
    variables=["tiger:tract"],
    state="06",              # state FIPS (California); required for tracts
).download()
```

TIGER cartographic-boundary files arrive in NAD83 (EPSG:4269) and are reprojected
to EPSG:4326. The `tiger:nation`, `tiger:state`, and `tiger:county` entities are
nationwide; `tiger:tract` is per-state and requires `state=`.

## Writing to disk

```python
fc = EarthLens(
    data_source="admin",
    variables=["natural_earth:countries"],
    path="out",              # writes out/admin_natural_earth_countries.gpkg
    file_format="geojson",   # or "gpkg" (default)
).download()
```
