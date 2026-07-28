# Biodiversity — usage

The four biodiversity backends are used like any other provider, through the `EarthLens` facade. This page covers
what is specific to the cluster: the shared request shape, the license warnings every result may carry, and the
helpers in `earthlens.biodiversity` for code that works across more than one of them.

For each backend's own options see [GBIF](../gbif/usage.md), [OBIS](../obis/usage.md), [WDPA](../wdpa/usage.md),
and [IUCN](../iucn/usage.md).

## The shared request shape

All four take a taxon or area selector over a bounding box; the occurrence backends also take a time window.

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="gbif",
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[35.0, 45.0],
    lon_lim=[-10.0, 5.0],
    path="data/gbif",
)
occurrences = lens.download()          # FeatureCollection of points, EPSG:4326
```

`download()` returns an **in-memory** result, not a list of paths:

| Backend | `OUTPUT_KIND` | Returns |
|---|---|---|
| `gbif`, `obis` | `vector` | `FeatureCollection` of occurrence points |
| `wdpa` | `vector` | `FeatureCollection` of protected-area polygons |
| `iucn` | `tabular` | `DataFrame` of assessment records |

None of the four is gridded, so the facade rejects `aggregate=` for all of them with `NotImplementedError`.

## Authentication

GBIF and OBIS search anonymously. WDPA and IUCN need a token:

```python
lens = EarthLens(data_source="wdpa", ...).authenticate(token="…")   # or WDPA_TOKEN
lens = EarthLens(data_source="iucn", ...).authenticate(token="…")   # or IUCN_TOKEN
```

Red List data is never shipped as package data — the token stays user-supplied.

## Handling license warnings

Every backend warns when a result carries attribution, non-commercial, or restricted-redistribution obligations.
All four raise the **same** `LicenseWarning` class, so one filter covers the cluster:

```python
import warnings
from earthlens.biodiversity import LicenseWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", LicenseWarning)
    result = lens.download()

for w in caught:
    print(w.message)
```

WDPA and IUCN warn on **every** download; GBIF and OBIS warn only when a returned record carries a `CC-BY-NC`
license. Treat a warning as a redistribution constraint on the data you just received, not as an error.

To fail loudly instead — useful in a pipeline that must not ingest restricted rows:

```python
warnings.simplefilter("error", LicenseWarning)
```

## Cross-backend helpers

`earthlens.biodiversity` holds the pieces the four backends share. Reach for these when you write code that spans
more than one of them.

### `wkt_from_bbox(space)`

Turns a `SpatialExtent` into the counter-clockwise `POLYGON((...))` WKT the occurrence and area APIs accept as a
`geometry=` filter.

```python
from earthlens.base import SpatialExtent
from earthlens.biodiversity import wkt_from_bbox

space = SpatialExtent.from_pairs(lat_lim=[35.0, 45.0], lon_lim=[-10.0, 5.0])
wkt_from_bbox(space)
# 'POLYGON ((5 35, 5 45, -10 45, -10 35, 5 35))'
```

### `occurrences_to_fc(records, *, lat_field, lon_field, columns)`

Maps occurrence rows — a `list[dict]` from `pygbif`, or the `DataFrame` `pyobis` returns — into a points
`FeatureCollection`.

```python
from earthlens.biodiversity import occurrences_to_fc

fc = occurrences_to_fc(
    records,
    lat_field="decimalLatitude",
    lon_field="decimalLongitude",
    columns={"species": "species", "eventDate": "event_date"},
)
```

A row with a missing coordinate gets a **null geometry** rather than an invalid `POINT (nan nan)`, and an empty
result keeps its column schema — so downstream code can concatenate results without special-casing the empty case.

### `warn_license(license_id, label, *, detail=None)`

Emits a `LicenseWarning` when `license_id` is in `RESTRICTIVE_LICENSES`. Returns whether it warned.

## Combining backends

Because GBIF and OBIS return the same shape, terrestrial and marine occurrences concatenate directly:

```python
import pandas as pd
from earthlens.core import EarthLens

box = dict(start="2020-01-01", end="2020-12-31",
           lat_lim=[35.0, 45.0], lon_lim=[-10.0, 5.0])

gbif = EarthLens(data_source="gbif", path="data/gbif", **box).download()
obis = EarthLens(data_source="obis", path="data/obis", **box).download()

combined = pd.concat([gbif, obis], ignore_index=True)
```

Both are `EPSG:4326`, so no reprojection is needed. See the worked example in
[Sea-turtle occurrences (GBIF + OBIS)](../../examples/showcase/species_occurrences_gbif_obis.ipynb).
