# NWP forecasts — usage

## Request shape

The NWP backend takes a `variables` mapping of **model key → list of
parameters** (the same shape as the GEE and STAC backends):

```python
from earthlens.earthlens import EarthLens

lens = EarthLens(
    data_source="nwp",
    variables={"gfs": ["temperature_2m", "precipitation_acc"]},
    start="2024-06-01",
    end="2024-06-02",              # cycle DATE range (inclusive)
    lat_lim=[40, 45],
    lon_lim=[-80, -75],
    path="out/gfs",
    steps=[0, 6, 12],              # forecast lead times, in hours
    mirror="aws",                  # cloud mirror (default "auto")
)
paths = lens.download()            # one bbox-cropped COG per (cycle, step)
```

Each parameter name resolves through the catalog to the centre's
selector — a Herbie `search` regex (`":TMP:2 m above ground:"`) for the
NOAA / ECMWF models, or a provider variable token (`"T_2M"`) for DWD
ICON. See [Catalog & install](catalog.md) for the available models and
their parameter names.

## Forecast steps: `steps=` vs `horizon=`

| kwarg | meaning |
|-------|---------|
| *(neither)* | only the **analysis step** `f000` — keeps a request small |
| `steps=[0, 6, 24]` | exactly these lead times (recommended — explicit) |
| `horizon=48` | `0..48` stepping on the model's `step_cadence_h` (e.g. every 3 h for GFS) |

A step beyond the model's `horizon_h` raises a `ValueError`. `horizon=`
expands on the model's published step cadence rather than blindly hourly,
so it won't request steps a coarse model never publishes. Any step the
model still doesn't carry on a given cycle is skipped per the
`errors=` policy below — it does not abort the download.

### Partial availability (`errors=`)

A `(cycle, step)` can be legitimately missing (the latest cycle isn't
published yet, or a model doesn't carry every step on every cycle).
`download(errors=...)` governs that:

| `errors=` | behaviour |
|-----------|-----------|
| `"warn"` (default) | log the miss, return the COGs that succeeded |
| `"skip"` | drop the miss silently |
| `"raise"` | abort the whole download on the first miss |

## Cloud mirror selection

`mirror=` chooses where the bytes come from:

| `mirror=` | NOAA (Herbie) | ECMWF Open Data |
|-----------|---------------|-----------------|
| `"auto"` (default) | the catalog `mirrors:` order | first known catalog mirror |
| `"aws"` | `aws` | `aws` |
| `"gcp"` | `google` | falls back to `ecmwf` |
| `"azure"` | `azure` | `azure` |
| `"origin"` | `nomads` | `ecmwf` |

## Output: bbox-cropped COGs

Each `(cycle, step)` yields one Cloud-Optimized GeoTIFF named
`{model}_{YYYYMMDDHH}_f{step:03d}.tif` in `path`. The download fetches
the **variable subset** GRIB2 (the bandwidth win), reads it with
`pyramids.grib.open_grib`, crops it to your bbox (global 0–360° grids
are shifted to −180..180 when the bbox reaches into negative
longitudes), and writes the COG.

!!! note "ICON-global grid"
    DWD's native ICON-global files are on an **icosahedral** grid, which
    does not crop as a regular lat/lon raster. The download path is
    correct, but for a croppable COG use a regular-lat/lon ICON product.

## Aggregating the forecast stack

Pass `aggregate=` to reduce the per-`(cycle, step)` COGs into windowed
composites (by **valid time** = `cycle + step`):

```python
from earthlens.aggregate import AggregationConfig

lens = EarthLens(
    data_source="nwp",
    variables={"gfs": ["temperature_2m"]},
    start="2024-06-01", end="2024-06-07",
    lat_lim=[40, 45], lon_lim=[-80, -75], path="out/gfs",
    steps=[0, 6, 12, 18],
)
paths = lens.download(
    aggregate=AggregationConfig(freq="1D", op="mean"),  # daily means
)
```

Aggregation requires a **single model** per request — different models
have different native grids and cannot be co-registered into one stack.
Issue one request per model.
