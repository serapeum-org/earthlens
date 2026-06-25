# GBIF — usage

Install the backend's optional SDK:

```bash
pip install earthlens[gbif]
```

## Download occurrences

Fetch a year of bird occurrences over a small bounding box:

```python
from earthlens import EarthLens

fc = EarthLens(
    data_source="gbif",
    variables=["birds"],            # friendly key -> taxonKey 212
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[51.4, 51.6],           # [lat_min, lat_max]
    lon_lim=[-0.2, 0.0],            # [lon_min, lon_max]
    path="out/gbif",               # GeoParquet written here
    max_records=5000,
).download()

print(type(fc))                     # a pyramids FeatureCollection (GeoDataFrame)
print(len(fc), "occurrences")
print(fc.geometry.iloc[0])          # a shapely Point in EPSG:4326
```

`download()` returns the in-memory `FeatureCollection` and writes
`out/gbif/gbif_occurrences.parquet`. Pass `file_format="gpkg"` or
`file_format="geojson"` to write those instead.

## Selecting taxa

```python
EarthLens(data_source="gbif", variables=["mammals"], ...)        # friendly key
EarthLens(data_source="gbif", variables=[212], ...)              # raw taxonKey
EarthLens(data_source="gbif", variables=["taxon:Panthera leo"], ...)  # name lookup
```

Multiple selectors are unioned into one search:

```python
EarthLens(data_source="gbif", variables=["birds", "mammals"], ...)
```

## Licensing

Honour the per-record license. When any record is `CC_BY_NC_4_0` the backend
raises a `LicenseWarning`:

```python
import warnings
from earthlens.biodiversity import LicenseWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", LicenseWarning)
    fc = EarthLens(data_source="gbif", variables=["birds"], ...).download()
nc_present = any(isinstance(w.message, LicenseWarning) for w in caught)
```

## Notes

- Search is anonymous — no credentials are needed.
- The bounding box's WKT must be small enough for GBIF's geometry limit
  (≤ 10,000 vertices); a plain bbox is always fine.
- Above `max_records`, use GBIF's asynchronous download API for the full set.
