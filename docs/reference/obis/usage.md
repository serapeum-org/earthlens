# OBIS — usage

Install the backend's optional SDK:

```bash
pip install earthlens[obis]
```

## Download marine occurrences

Fetch ocean-sunfish occurrences over a North-Atlantic bounding box:

```python
from earthlens import EarthLens

fc = EarthLens(
    data_source="obis",
    variables=["ocean-sunfish"],    # friendly key -> "Mola mola"
    start="2010-01-01",
    end="2020-12-31",
    lat_lim=[40.0, 55.0],
    lon_lim=[-20.0, 0.0],
    path="out/obis",
    size=2000,                       # max records to request
).download()

print(len(fc), "occurrences")
print(fc.geometry.iloc[0])           # a shapely Point in EPSG:4326
```

`download()` returns the in-memory `FeatureCollection` and writes
`out/obis/obis_occurrences.parquet` (or `file_format="gpkg"` / `"geojson"`).

## Selecting species

```python
EarthLens(data_source="obis", variables=["blue-whale"], ...)         # friendly key
EarthLens(data_source="obis", variables=["species:Mola mola"], ...)  # explicit name
```

## Licensing

```python
import warnings
from earthlens.biodiversity import LicenseWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", LicenseWarning)
    fc = EarthLens(data_source="obis", variables=["blue-whale"], ...).download()
```

## Notes

- Search is anonymous — no credentials are needed.
- For very large pulls (> 10,000 records) OBIS uses an internal id cursor;
  raise `size` to fetch more per request.
