# FDSN seismic events — usage

This page walks through fetching seismic events with the `fdsn`
backend. For background and the network list see
[Introduction](introduction.md); the rendered API is the
[Reference](fdsn.md) page.

## Install

The backend needs [`obspy`](https://docs.obspy.org/) — pulled in by the
`fdsn` extra:

```bash
pip install earthlens[fdsn]
```

## Quickstart — recent global M5+ events

```python
from earthlens import EarthLens

events = EarthLens(
    variables=["USGS"],          # the network(s) to query — see below
    data_source="fdsn",
    start="2024-01-01",
    end="2024-01-31",
    lat_lim=[-90, 90],
    lon_lim=[-180, 180],
    min_magnitude=5.0,
    path="./out",
).download()

print(len(events), "events")
print(events[["time", "magnitude", "depth_km", "geometry"]].head())
```

`download()` returns a pyramids `FeatureCollection` (a
`geopandas.GeoDataFrame` subclass), so every pandas / geopandas method
works on it directly. It also writes `out/usgs.gpkg`.

## Choosing the network(s) — `variables`

For this backend `variables` is the list of seismic **networks**, not
data-variable names. This is an intentional, documented overload: the
`EarthLens` facade makes `variables` a required argument on every call,
so adding a separate `providers=` keyword would only force a redundant
placeholder. Pass one or more network keys:

```python
# one network (the default if you pass an empty list is ["USGS"])
EarthLens(variables=["USGS"], data_source="fdsn", ...)

# several networks — the results are unioned into one FeatureCollection
EarthLens(variables=["USGS", "EMSC", "INGV"], data_source="fdsn", ...)
```

Valid keys are `USGS`, `EMSC`, `INGV`, `EARTHSCOPE`, `ISC`, `GEONET`.
An unknown key raises with a did-you-mean hint
(`Catalog().get_provider("USG")` → *Did you mean 'USGS'?*).

## Filtering the query

Query filters arrive as explicit keyword arguments (forwarded verbatim
by the facade's `**backend_kwargs`):

| Keyword | Meaning | Default |
|---|---|---|
| `min_magnitude` / `max_magnitude` | magnitude bounds | `4.5` / `None` |
| `min_depth` / `max_depth` | depth bounds, **kilometres** | `None` |
| `magnitude_type` | restrict to e.g. `"Mw"` | `None` (any) |
| `event_type` | restrict to e.g. `"earthquake"` | `None` (any) |
| `orderby` | `"time"`, `"time-asc"`, `"magnitude"`, `"magnitude-asc"` | `"time"` |
| `limit` | max events per network | `None` |
| `file_format` | `"gpkg"` or `"geojson"` | `"gpkg"` |

The spatial window comes from `lat_lim` / `lon_lim` and the temporal
window from `start` / `end`. FDSN issues one query spanning the whole
`[start, end]` window — it does not chunk by day or month — so
`temporal_resolution` is irrelevant here (it carries the sentinel
`"all"`).

```python
# shallow Italian earthquakes, smallest first
EarthLens(
    variables=["INGV"],
    data_source="fdsn",
    start="2023-01-01",
    end="2023-12-31",
    lat_lim=[36, 47],
    lon_lim=[6, 19],
    min_magnitude=2.0,
    max_depth=30.0,
    event_type="earthquake",
    orderby="magnitude-asc",
    path="./out",
).download()
```

## The returned FeatureCollection

CRS `EPSG:4326`, one row per event. Columns: `event_id`, `time`
(UTC), `longitude`, `latitude`, `depth_km`, `magnitude`,
`magnitude_type`, `event_type`, `status`, `provider`, `geometry`
(`shapely.Point`). An empty result (a quiet region/time) is returned as
an empty FeatureCollection with exactly these columns — not an error —
so downstream `concat` / `to_file` never breaks.

### Plotting

```python
import matplotlib.pyplot as plt

events = EarthLens(variables=["USGS"], data_source="fdsn",
                   start="2024-01-01", end="2024-03-31",
                   lat_lim=[-90, 90], lon_lim=[-180, 180],
                   min_magnitude=5.5, path="./out").download()

ax = events.plot(markersize=events["magnitude"] ** 2, alpha=0.5)
ax.set_title("M5.5+ earthquakes, Q1 2024")
plt.show()
```

### Writing to disk

`download()` writes one file per network automatically (named after the
network, e.g. `usgs.gpkg`). To write the combined collection yourself:

```python
events.to_file("all_events.gpkg", driver="GPKG")     # GeoPackage
events.to_file("all_events.geojson", driver="GeoJSON")  # GeoJSON
```

## Aggregation is not supported

FDSN output is vector, so the `aggregate=` argument is rejected:

```python
EarthLens(variables=["USGS"], data_source="fdsn", ...).download(aggregate=cfg)
# NotImplementedError: aggregate= is not supported ... (OUTPUT_KIND='vector')
```

The aggregator only reduces gridded raster outputs. Post-process the
returned FeatureCollection directly instead (it is a GeoDataFrame).

## EarthScope token (optional)

EarthScope's restricted endpoints can take an access token; the public
event service does not need one. If you have a token:

```python
EarthLens(variables=["EARTHSCOPE"], data_source="fdsn",
          earthscope_token="…", ...)   # or set EARTHSCOPE_TOKEN / ~/.earthscope_token
```
