# Bounding a result with `limit=`

Most backends will happily return more than you asked for. A broad taxon over a
wide bbox, a multi-year window, a country-scale OSM query — each can produce
more rows than fit comfortably in memory, and you often only want a sample.

`limit=` is the shared way to cap that. Pass it to `download()`:

```python
from earthlens.core import EarthLens

el = EarthLens(
    "obis",
    start="2020-01-01",
    end="2020-12-31",
    variables=["blue-whale"],
    lat_lim=[-60.0, 60.0],
    lon_lim=[-180.0, 180.0],
)
occurrences = el.download(limit=500)
```

The facade forwards the keyword to the bound backend, so this works the same
whether you go through `EarthLens` or construct the backend directly.

## What the cap actually bounds

The cap is a **total across the request**, not a per-page or per-item size, and
it is exact: a request capped at 500 returns 500 rows, not the next multiple of
whatever page size the provider uses.

Where backends differ is in how much work the cap avoids, and that difference is
worth knowing before you rely on it:

| Behaviour | What it means | Backends |
|---|---|---|
| **Stops the work** | Items past the cap are never fetched. The cap bounds requests and memory, not just the return value. | admin, climate_indices, drought (USDM), eea_aq, nrel, obis, openaq, osm, pvgis, sensor_community, tropycal (across basins / storms), wdpa |
| **Pushed to the service** | The cap goes into the provider's own query, so the surplus is never transferred at all. | fdsn (per network) |
| **Trims the result** | The provider answers the whole request in one call, so the cap bounds what you get back but not what was fetched. | argo, usgs_water, tropycal (within one basin) |

For the third group, narrowing the request is what reduces transfer — a tighter
date window, a smaller bbox, or (for usgs_water) the constructor's own
`limit=`, which the service applies per request.

## Rules that hold everywhere

- `limit=0` and negative values raise `ValueError`. A request for no rows is a
  caller bug, not a cheap no-op to serve.
- `limit=True` raises `TypeError`. A bool is a mistake, not a cap of one.
- `limit=None` (the default) means no cap.
- A cap larger than the result set is not an error; you get everything.

## Backends that take no `limit=`

Two refuse it deliberately rather than accepting a cap they cannot honour:

- **erddap** resolves to a single dataset, so there is no per-item loop a cap
  could stop, and every tabledap row has been transferred by the time a frame
  exists. A cap there could only trim. The real bound is ERDDAP's server-side
  `orderByLimit`; narrow the date window until that is wired.
- **drought's raster transports** write files rather than rows, so a row cap
  does not describe them. Passing `limit=` to one raises `ValueError` instead of
  being silently ignored.

## Streaming instead of capping

When you want to process results without holding them all, `iter_download()`
yields per-item fragments and takes the same cap:

```python
for frame in el.iter_download(limit=1000):
    process(frame)
```
