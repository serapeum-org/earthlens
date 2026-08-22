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
from earthlens.core import EarthLens

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
| `min_magnitude` / `max_magnitude` | magnitude bounds | `None` / `None` |
| `min_depth` / `max_depth` | depth bounds, **kilometres** | `None` |
| `magnitude_type` | restrict to e.g. `"Mw"` | `None` (any) |
| `event_type` | restrict to e.g. `"earthquake"` | `None` (any) |
| `orderby` | `"time"`, `"time-asc"`, `"magnitude"`, `"magnitude-asc"` | `"time"` |
| `limit` | max events per network | `None` |
| `file_format` | `"gpkg"` or `"geojson"` | `"gpkg"` |
| `with_shakemap` | also write each USGS event's ShakeMap as GeoTIFF | `False` |
| `shakemap_layers` | which ShakeMap grids to write | `["mmi_mean"]` |
| `max_shakemap_events` | ceiling on events fetched a ShakeMap for | `100` |

`min_magnitude=None` (the default) falls back **per network** to that
provider's catalog floor — USGS / EMSC / EarthScope / ISC use `4.5`,
INGV uses `2.0`, GeoNet uses `3.0` — so each regional network keeps a
sensible default. Pass an explicit number to override every network
with one bound.

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

## ShakeMap rasters (USGS only)

Pass `with_shakemap=True` to also pull each event's gridded **ShakeMap**
and write it as a GeoTIFF beside the vector output:

```python
events = EarthLens(
    variables=["USGS"],
    data_source="fdsn",
    start="2023-02-06",
    end="2023-02-07",
    lat_lim=[35.0, 39.0],
    lon_lim=[35.0, 39.0],
    path="out",
    min_magnitude=7.0,
    with_shakemap=True,
).download()
```

That writes `out/shakemap/<event-id>/mmi_mean.tif` per event, alongside
the usual `out/usgs.gpkg`. The return value is unchanged — still the
event `FeatureCollection`; the rasters are a side effect.

**USGS only.** ShakeMap is a USGS ComCat product and is not part of the
FDSN event standard, so a non-USGS network in the same request still
contributes events but no rasters (each such network is logged once).
ShakeMap costs one extra request and a multi-megabyte archive **per
event** — measured at 8 MB and 20 MB for the two events of the 2023
Kahramanmaraş sequence, and it scales with the event's footprint — so a
broad window is gigabytes. `max_shakemap_events` (default `100`) caps how many
events one call will fetch: past the ceiling the rest are skipped with a
warning naming the count — never silently. Raise it deliberately, or
narrow the query with `limit=` / `min_magnitude=`.

### Re-running

Each event directory carries a hidden `.shakemap.json` manifest recording which
layers that event's archive actually produced. A re-run consults it, so a repeat
costs nothing — including for an event whose archive simply does not carry a
layer you asked for, which would otherwise be refetched forever.

Three consequences worth knowing:

- **Reuse requires the earlier run to have asked for at least what you now ask
  for.** The manifest merges across runs, so asking for `mmi_mean`, then
  `pga_mean`, then both is served from disk — nothing already fetched is
  discarded or fetched twice.
- **A layer the archive did not carry is re-checked after a week.** That covers
  both an event with no ShakeMap at all and one whose archive carried only some
  of the layers you asked for — ShakeMap is generated minutes to hours after an
  event, so neither is cached as final. A run returning fewer layers than
  requested says so in a warning. An event with no grid at all leaves a
  directory holding only the hidden manifest.
- **A revised grid is not detected.** USGS republishes ShakeMaps for years (the
  2023 Kahramanmaraş grid is on its twelfth version). A run with the rasters
  already on disk does not re-check; pass `download(force=True)` to refetch.
- **The ceiling counts fetches, not events.** Events already on disk cost no
  budget, so re-running advances through a long list. An event that fails every
  time keeps its place rather than being skipped.

To clear one event's cache, delete its directory under `out/shakemap/`.

By default only `mmi_mean` (macroseismic intensity) is written. The
archive carries fourteen grids — `mmi`, `pga`, `pgv`, `psa0p3`,
`psa0p6`, `psa1p0`, `psa3p0`, each as `_mean` and `_std` — and any
subset can be requested:

```python
EarthLens(
    ...,
    with_shakemap=True,
    shakemap_layers=["mmi_mean", "pga_mean", "pgv_mean"],
).download()
```

The grids are published as ESRI float rasters carrying no projection;
earthlens assigns `EPSG:4326` (their true graticule) during the
conversion to GeoTIFF.

PAGER is **not** included: its ComCat product ships PDFs, PNGs, and
JSON summaries, with no raster grid to write.

## Aggregation is not supported

FDSN output is vector, so the `aggregate=` argument is rejected:

```python
EarthLens(variables=["USGS"], data_source="fdsn", ...).download(aggregate=cfg)
# NotImplementedError: aggregate= is not supported ... (OUTPUT_KIND='vector')
```

The aggregator only reduces gridded raster outputs. Post-process the
returned FeatureCollection directly instead (it is a GeoDataFrame).

This holds with `with_shakemap=True` too. That flag does not change
`OUTPUT_KIND` — `download()` still returns the event FeatureCollection,
and the ShakeMap grids are an on-disk side effect with no time axis to
reduce over.

## EarthScope token (optional)

EarthScope's restricted endpoints can take an access token; the public
event service does not need one. If you have a token:

```python
EarthLens(variables=["EARTHSCOPE"], data_source="fdsn",
          earthscope_token="…", ...)   # or set EARTHSCOPE_TOKEN / ~/.earthscope_token
```
