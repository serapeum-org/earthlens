# Protected Planet (WDPA) — usage

The backend uses core `requests`, so there is **no extra to install**. You do
need a Protected Planet v4 token.

## Authentication

Request a token at
[api.protectedplanet.net/request](https://api.protectedplanet.net/request),
then either set it in the environment:

```bash
export WDPA_TOKEN="your-token"     # PowerShell: $env:WDPA_TOKEN = "your-token"
```

or pass it as a keyword argument (`token="your-token"`). A missing token
raises `AuthenticationError` naming `WDPA_TOKEN`.

## Download protected areas

Fetch a country's protected areas as polygons:

```python
from earthlens import EarthLens

fc = EarthLens(
    data_source="wdpa",
    variables=["KEN"],              # ISO3 country code; or ["country:KEN"]
    start="2024-01-01",            # WDPA is not time-filtered; frames the request
    end="2024-12-31",
    lat_lim=[-90.0, 90.0],
    lon_lim=[-180.0, 180.0],
    path="out/wdpa",
    # token="your-token",          # or rely on WDPA_TOKEN
).download()

print(len(fc), "protected areas")
print(fc.geometry.iloc[0].geom_type)   # 'Polygon' or 'MultiPolygon'
```

`download()` returns the polygon `FeatureCollection` and writes
`out/wdpa/wdpa_protected_areas.parquet` (or `file_format="gpkg"` /
`"geojson"`). Fetch one area by its WDPA id with a numeric selector:

```python
EarthLens(data_source="wdpa", variables=["555"], ...)
```

## Licensing

WDPA is not Creative Commons — commercial use needs written permission from
UNEP-WCMC and redistribution is restricted. Every download raises a
`LicenseWarning`; cite the database as required by the
[Protected Planet terms](https://www.protectedplanet.net/en/legal).
