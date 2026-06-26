# Bathymetry — available datasets

The `bathymetry` backend ships three curated DEM rows. Pick one with the
`dataset=` argument. Every row is a static global elevation grid served as
a NOAA ERDDAP `griddap` coverage, with a single elevation band in **metres
relative to sea level** (negative = below sea level).

| `dataset=` | DEM | Native resolution | Band | Longitude | Server |
|---|---|---|---|---|---|
| `gebco_2020` | GEBCO 2020 (topography + bathymetry) | 15 arc-second | `elevation` | −180..180 | NOAA CoastWatch ERDDAP |
| `etopo1_ice` | ETOPO1 global relief, **Ice Surface** | 1 arc-minute | `z` | −180..180 | NOAA upwell ERDDAP |
| `etopo1_bedrock` | ETOPO1 global relief, **Bedrock** | 1 arc-minute | `z` | −180..180 | NOAA upwell ERDDAP |

The ETOPO variant is the dataset id, not a `variables` knob: choose
`etopo1_ice` for the top of the ice sheets or `etopo1_bedrock` for their
base.

## Facade aliases

Two friendly aliases route to the same backend, so a script reads the way
you think about the data:

- `data_source="gebco"` — then pass `dataset="gebco_2020"`.
- `data_source="etopo"` — then pass `dataset="etopo1_ice"` or
  `dataset="etopo1_bedrock"`.

`data_source="bathymetry"` works with any of the three `dataset=` ids.

## Listing the ids programmatically

```python
from earthlens.bathymetry import Catalog

catalog = Catalog()
sorted(catalog.datasets)        # ['etopo1_bedrock', 'etopo1_ice', 'gebco_2020']
catalog.get("gebco_2020").variable      # 'elevation'
catalog.get("etopo1_ice").native_resolution    # '1 arc-minute'
```

## Versions

These are the versions cleanly subsettable over the public ERDDAP
`griddap` transport today (GEBCO 2020 and ETOPO1). Newer releases (ETOPO
2022, later GEBCO grids) are not exposed as a bbox-subsettable ERDDAP
coverage on the public hosts; when they are, they can be added as extra
catalog rows without any backend change.
